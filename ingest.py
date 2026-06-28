import os
import json
import logging
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests

BASE_URL = "https://hackathon.prod.pulsefoundry.ai"
FACILITY_IDS = [101, 102, 103]
DB_PATH = "wound_care.db"
PICKLE_PATH = "data/raw_patient_data.pkl"
MAX_WORKERS = 8
MAX_HTTP_RETRIES = 3   # retries per job attempt (429 / network glitches)
MAX_JOB_ATTEMPTS = 5   # job-level retries before dead-letter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_claim_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS fetch_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key       TEXT    UNIQUE NOT NULL,
    endpoint      TEXT    NOT NULL,
    params_json   TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_jobs_status ON fetch_jobs(status);

-- Per-job outcome log: one row per completed job (success or dead-letter).
CREATE TABLE IF NOT EXISTS ingest_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL,
    endpoint      TEXT    NOT NULL,
    patient_id    TEXT    NOT NULL,
    rows_returned INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL,
    logged_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_log_zeros ON ingest_log(endpoint, rows_returned);

CREATE TABLE IF NOT EXISTS raw_diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    patient_id TEXT    NOT NULL,
    raw_json   TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    patient_id TEXT    NOT NULL,
    raw_json   TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    patient_id TEXT    NOT NULL,
    raw_json   TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    patient_id TEXT    NOT NULL,
    raw_json   TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    facility_id INTEGER,
    patient_id TEXT UNIQUE,
    first_name TEXT,
    last_name TEXT,
    birth_date TEXT,
    gender TEXT,
    primary_payer_code TEXT,
    last_modified_at TEXT,
    is_new_admission INTEGER
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY,
    patient_id TEXT,
    icd10_code TEXT,
    icd10_description TEXT,
    clinical_status TEXT,
    onset_date TEXT,
    last_modified_at TEXT
);

CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY,
    patient_id TEXT,
    payer_name TEXT,
    payer_code TEXT,
    payer_type TEXT,
    effective_from TEXT,
    effective_to TEXT,
    last_modified_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER,
    org_id TEXT,
    pcc_note_id INTEGER,
    note_type TEXT,
    effective_date TEXT,
    note_text TEXT,
    created_by TEXT,
    note_label TEXT,
    sync_version INTEGER,
    is_current INTEGER
);

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER,
    org_id TEXT,
    pcc_assessment_id INTEGER,
    assessment_type TEXT,
    status TEXT,
    assessment_date TEXT,
    completion_date TEXT,
    template_id INTEGER,
    assessment_type_description TEXT,
    raw_json TEXT,
    sync_version INTEGER,
    is_current INTEGER
);

CREATE TABLE IF NOT EXISTS sync_log (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    patients_fetched INTEGER,
    since_ts TEXT
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Rate-aware HTTP client: backoff + jitter + Retry-After
# ---------------------------------------------------------------------------

def _jitter_backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Full jitter: uniform in [0, min(cap, base * 2^attempt)]."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def fetch_url(endpoint: str, params: dict) -> Optional[list]:
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(MAX_HTTP_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 1))
                wait = max(retry_after, _jitter_backoff(attempt))
                log.debug("429 %s — waiting %.1fs (http attempt %d)", endpoint, wait, attempt + 1)
                time.sleep(wait)
                continue
            log.warning("HTTP %d %s %s", resp.status_code, endpoint, params)
            return None
        except requests.RequestException as exc:
            wait = _jitter_backoff(attempt)
            log.warning("Network error %s: %s — sleeping %.1fs", endpoint, exc, wait)
            time.sleep(wait)
    log.error("HTTP exhausted %d retries: %s %s", MAX_HTTP_RETRIES, endpoint, params)
    return None


# ---------------------------------------------------------------------------
# Job queue operations
# ---------------------------------------------------------------------------

def _open(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def claim_job(db_path: str) -> Optional[dict]:
    """Atomically claim one pending job. Returns None when queue is drained."""
    with _claim_lock:
        c = _open(db_path)
        try:
            row = c.execute(
                "SELECT id, endpoint, params_json, attempt_count "
                "FROM fetch_jobs WHERE status='pending' LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE fetch_jobs SET status='running', updated_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), row["id"]),
            )
            c.commit()
            return dict(row)
        finally:
            c.close()


def _mark_success(db_path: str, job_id: int):
    c = _open(db_path)
    try:
        c.execute(
            "UPDATE fetch_jobs SET status='success', updated_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), job_id),
        )
        c.commit()
    finally:
        c.close()


def _requeue_or_dead(db_path: str, job_id: int, attempt_count: int, error: str) -> str:
    """Increment attempt count; requeue if under limit, dead-letter otherwise. Returns new status."""
    next_count = attempt_count + 1
    now = datetime.utcnow().isoformat()
    if next_count >= MAX_JOB_ATTEMPTS:
        status = "dead"
        log.error("Job %d dead-lettered after %d attempts: %s", job_id, next_count, error)
    else:
        status = "pending"
        log.warning(
            "Job %d failed (attempt %d/%d): %s — requeueing",
            job_id, next_count, MAX_JOB_ATTEMPTS, error,
        )
    c = _open(db_path)
    try:
        c.execute(
            "UPDATE fetch_jobs SET status=?, attempt_count=?, last_error=?, updated_at=? WHERE id=?",
            (status, next_count, error[:500], now, job_id),
        )
        c.commit()
    finally:
        c.close()
    return status


def _write_ingest_log(
    db_path: str, job_id: int, endpoint: str, patient_id: str, rows_returned: int, status: str
):
    """Write one row to ingest_log."""
    c = _open(db_path)
    try:
        c.execute(
            "INSERT INTO ingest_log (job_id, endpoint, patient_id, rows_returned, status, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, endpoint, patient_id, rows_returned, status, datetime.utcnow().isoformat()),
        )
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Raw landing + structured table writers (one per endpoint)
# ---------------------------------------------------------------------------

def _write_diagnoses(c, job_id, params, rows, now):
    pid = params.get("patient_id", "")
    c.executemany(
        "INSERT INTO raw_diagnoses (job_id, patient_id, raw_json, fetched_at) VALUES (?,?,?,?)",
        [(job_id, pid, json.dumps(r), now) for r in rows],
    )
    c.executemany("""
        INSERT OR REPLACE INTO diagnoses
          (id, patient_id, icd10_code, icd10_description, clinical_status, onset_date, last_modified_at)
        VALUES (:id, :patient_id, :icd10_code, :icd10_description, :clinical_status, :onset_date, :last_modified_at)
    """, rows)


def _write_coverage(c, job_id, params, rows, now):
    pid = params.get("patient_id", "")
    c.executemany(
        "INSERT INTO raw_coverage (job_id, patient_id, raw_json, fetched_at) VALUES (?,?,?,?)",
        [(job_id, pid, json.dumps(r), now) for r in rows],
    )
    c.executemany("""
        INSERT OR REPLACE INTO coverage
          (id, patient_id, payer_name, payer_code, payer_type, effective_from, effective_to, last_modified_at)
        VALUES (:id, :patient_id, :payer_name, :payer_code, :payer_type,
                :effective_from, :effective_to, :last_modified_at)
    """, rows)


def _write_notes(c, job_id, params, rows, now):
    pid = str(params.get("patient_id", ""))
    c.executemany(
        "INSERT INTO raw_notes (job_id, patient_id, raw_json, fetched_at) VALUES (?,?,?,?)",
        [(job_id, pid, json.dumps(r), now) for r in rows],
    )
    c.executemany("""
        INSERT OR REPLACE INTO notes
          (id, patient_id, org_id, pcc_note_id, note_type, effective_date,
           note_text, created_by, note_label, sync_version, is_current)
        VALUES (:id, :patient_id, :org_id, :pcc_note_id, :note_type, :effective_date,
                :note_text, :created_by, :note_label, :sync_version, :is_current)
    """, rows)


def _write_assessments(c, job_id, params, rows, now):
    pid = str(params.get("patient_id", ""))
    c.executemany(
        "INSERT INTO raw_assessments (job_id, patient_id, raw_json, fetched_at) VALUES (?,?,?,?)",
        [(job_id, pid, json.dumps(r), now) for r in rows],
    )
    c.executemany("""
        INSERT OR REPLACE INTO assessments
          (id, patient_id, org_id, pcc_assessment_id, assessment_type, status,
           assessment_date, completion_date, template_id, assessment_type_description,
           raw_json, sync_version, is_current)
        VALUES (:id, :patient_id, :org_id, :pcc_assessment_id, :assessment_type, :status,
                :assessment_date, :completion_date, :template_id, :assessment_type_description,
                :raw_json, :sync_version, :is_current)
    """, rows)


_WRITERS = {
    "/pcc/diagnoses":   _write_diagnoses,
    "/pcc/coverage":    _write_coverage,
    "/pcc/notes":       _write_notes,
    "/pcc/assessments": _write_assessments,
}


def _persist_result(db_path: str, job_id: int, endpoint: str, params: dict, rows: list):
    writer = _WRITERS.get(endpoint)
    if writer is None:
        log.warning("No writer for endpoint %s", endpoint)
        return
    c = _open(db_path)
    try:
        writer(c, job_id, params, rows, datetime.utcnow().isoformat())
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Bounded worker pool
# ---------------------------------------------------------------------------

def _process_job(db_path: str, job: dict):
    job_id = job["id"]
    endpoint = job["endpoint"]
    params = json.loads(job["params_json"])
    attempt_count = job["attempt_count"]
    patient_id = str(params.get("patient_id", ""))

    rows = fetch_url(endpoint, params)
    if rows is None:
        new_status = _requeue_or_dead(db_path, job_id, attempt_count, f"no response from {endpoint}")
        if new_status == "dead":
            _write_ingest_log(db_path, job_id, endpoint, patient_id, 0, "dead")
        return

    _persist_result(db_path, job_id, endpoint, params, rows)
    _mark_success(db_path, job_id)
    _write_ingest_log(db_path, job_id, endpoint, patient_id, len(rows), "success")


def _worker(db_path: str) -> int:
    """Claim and process jobs until queue is empty. Returns count processed."""
    count = 0
    while True:
        job = claim_job(db_path)
        if job is None:
            break
        _process_job(db_path, job)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Manifest generator
# ---------------------------------------------------------------------------

def generate_manifest(db_path: str, since: Optional[str] = None) -> list[dict]:
    """Fetch patient lists, write patients table, enqueue per-patient detail jobs."""
    log.info("=== Generating manifest (since=%s) ===", since or "full")

    all_patients: list[dict] = []
    for fid in FACILITY_IDS:
        params: dict = {"facility_id": fid}
        if since:
            params["since"] = since
        patients = fetch_url("/pcc/patients", params) or []
        log.info("Facility %d: %d patients", fid, len(patients))
        all_patients.extend(patients)

    if all_patients:
        c = _open(db_path)
        try:
            c.executemany("""
                INSERT OR REPLACE INTO patients
                  (id, facility_id, patient_id, first_name, last_name, birth_date,
                   gender, primary_payer_code, last_modified_at, is_new_admission)
                VALUES
                  (:id, :facility_id, :patient_id, :first_name, :last_name, :birth_date,
                   :gender, :primary_payer_code, :last_modified_at, :is_new_admission)
            """, all_patients)
            c.commit()
        finally:
            c.close()

    now = datetime.utcnow().isoformat()
    job_rows = []
    for p in all_patients:
        # Payer Short-Circuit Optimization:
        # Skip enqueuing downstream fetch jobs for non-Medicare Part B (MCB) patients
        if p.get("primary_payer_code") != "MCB":
            continue

        pid_str = p["patient_id"]
        pid_int = p["id"]
        note_params = {"patient_id": pid_int}
        assess_params = {"patient_id": pid_int}
        if since:
            note_params["since"] = since
            assess_params["since"] = since
        job_rows += [
            (f"diagnoses:{pid_str}",   "/pcc/diagnoses",   json.dumps({"patient_id": pid_str}), now, now),
            (f"coverage:{pid_str}",    "/pcc/coverage",    json.dumps({"patient_id": pid_str}), now, now),
            (f"notes:{pid_int}",       "/pcc/notes",       json.dumps(note_params),             now, now),
            (f"assessments:{pid_int}", "/pcc/assessments", json.dumps(assess_params),           now, now),
        ]

    c = _open(db_path)
    try:
        c.executemany(
            "INSERT OR IGNORE INTO fetch_jobs (job_key, endpoint, params_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            job_rows,
        )
        c.commit()
        log.info("Enqueued %d fetch jobs (%d MCB patients × 4 endpoints)", len(job_rows), len(job_rows) // 4)
    finally:
        c.close()

    return all_patients


# ---------------------------------------------------------------------------
# Backwards Compatibility: Export to Pandas Pickle
# ---------------------------------------------------------------------------

def export_to_pandas_pickle(db_path: str, pickle_path: str):
    """
    Queries the SQLite database and exports the data in the exact same format
    expected by extract.py to maintain pipeline compatibility.
    """
    import pandas as pd
    log.info("Exporting SQLite raw landing tables to pandas cache %s...", pickle_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    patients_rows = conn.execute("SELECT * FROM patients").fetchall()
    patients = [dict(r) for r in patients_rows]
    
    for p in patients:
        pid_str = p["patient_id"]
        pid_int = p["id"]
        
        # Cast types to match original schema
        p["is_new_admission"] = bool(p["is_new_admission"])
        
        # Fetch diagnoses
        diag_rows = conn.execute("SELECT * FROM diagnoses WHERE patient_id = ?", (pid_str,)).fetchall()
        p["diagnoses"] = [dict(r) for r in diag_rows]
        
        # Fetch coverage
        cov_rows = conn.execute("SELECT * FROM coverage WHERE patient_id = ?", (pid_str,)).fetchall()
        p["coverage"] = [dict(r) for r in cov_rows]
        
        # Fetch notes
        note_rows = conn.execute("SELECT * FROM notes WHERE patient_id = ?", (pid_int,)).fetchall()
        p["notes"] = []
        for r in note_rows:
            d = dict(r)
            d["is_current"] = bool(d["is_current"])
            p["notes"].append(d)
        
        # Fetch assessments
        assess_rows = conn.execute("SELECT * FROM assessments WHERE patient_id = ?", (pid_int,)).fetchall()
        p["assessments"] = []
        for r in assess_rows:
            d = dict(r)
            d["is_current"] = bool(d["is_current"])
            p["assessments"].append(d)
            
        # Calculate pre-rejections (0-token optimization)
        p["pre_reject"] = p["primary_payer_code"] != "MCB"
        p["pre_reject_reason"] = "Payer is not Medicare Part B" if p["pre_reject"] else ""
        
    conn.close()
    
    df = pd.DataFrame(patients)
    os.makedirs(os.path.dirname(pickle_path), exist_ok=True)
    df.to_pickle(pickle_path)
    log.info("Export complete. Saved %d patient records to %s", len(df), pickle_path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_ingestion(since: Optional[str] = None, db_path: str = DB_PATH) -> sqlite3.Connection:
    started = datetime.utcnow().isoformat()

    conn = init_db(db_path)

    # Recover stale 'running' jobs left by a previous crashed run
    conn.execute(
        "UPDATE fetch_jobs SET status='pending', updated_at=? WHERE status='running'",
        (started,),
    )
    conn.commit()

    # Phase 1: build the manifest
    all_patients = generate_manifest(db_path, since)

    # Phase 2: drain the queue with a bounded worker pool
    log.info("=== Draining queue with %d workers ===", MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_worker, db_path) for _ in range(MAX_WORKERS)]
        total_processed = sum(f.result() for f in as_completed(futures))

    dead_count = conn.execute(
        "SELECT COUNT(*) FROM fetch_jobs WHERE status='dead'"
    ).fetchone()[0]
    if dead_count:
        log.warning("%d jobs dead-lettered — inspect: SELECT * FROM fetch_jobs WHERE status='dead'", dead_count)

    finished = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO sync_log (started_at, finished_at, patients_fetched, since_ts) VALUES (?,?,?,?)",
        (started, finished, len(all_patients), since),
    )
    conn.commit()

    # Backwards Compatibility: Export to Pandas Pickle
    export_to_pandas_pickle(db_path, PICKLE_PATH)

    log.info(
        "=== Ingestion complete: %d patients, %d jobs processed, %d dead-lettered ===",
        len(all_patients), total_processed, dead_count,
    )
    return conn


if __name__ == "__main__":
    run_ingestion()
