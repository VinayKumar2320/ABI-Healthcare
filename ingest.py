import os
import time
import pandas as pd
import requests
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

BASE_URL = "https://hackathon.prod.pulsefoundry.ai"
FACILITIES = [101, 102, 103]
DATA_DIR = "data"
CACHE_FILE = os.path.join(DATA_DIR, "raw_patient_data.pkl")

def make_request(url, params=None):
    """
    Makes an HTTP GET request, handling rate limits (HTTP 429) with dynamic backoff
    using the Retry-After header.
    """
    while True:
        try:
            response = requests.get(url, params=params)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 3))
                logging.warning(f"Rate limit hit (429) on {url}. Retrying after {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            elif response.status_code == 422:
                logging.error(f"Validation error (422) on {url} with params {params}: {response.text}")
                response.raise_for_status()
            elif response.status_code != 200:
                logging.error(f"Error {response.status_code} on {url}: {response.text}")
                response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error: {e}. Retrying in 2 seconds...")
            time.sleep(2)

def ingest_data():
    """
    Ingests patient data from the mock PCC API, filters by Medicare Part B,
    fetches downstream records, and caches the result.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    all_patient_records = []

    for facility_id in FACILITIES:
        logging.info(f"Fetching patients for Facility {facility_id}...")
        patients = make_request(f"{BASE_URL}/pcc/patients", params={"facility_id": facility_id})
        logging.info(f"Found {len(patients)} patients in Facility {facility_id}.")

        for idx, patient in enumerate(patients):
            patient_id_str = patient.get("patient_id")  # e.g., "FA-001"
            patient_id_int = patient.get("id")          # e.g., 1
            payer_code = patient.get("primary_payer_code")

            record = {
                "id": patient_id_int,
                "patient_id": patient_id_str,
                "facility_id": facility_id,
                "first_name": patient.get("first_name"),
                "last_name": patient.get("last_name"),
                "birth_date": patient.get("birth_date"),
                "gender": patient.get("gender"),
                "primary_payer_code": payer_code,
                "last_modified_at": patient.get("last_modified_at"),
                "is_new_admission": patient.get("is_new_admission"),
                "pre_reject": False,
                "pre_reject_reason": "",
                "diagnoses": [],
                "coverage": [],
                "notes": [],
                "assessments": []
            }

            logging.info(f"[{idx+1}/{len(patients)}] Processing patient {patient_id_str} (Internal ID: {patient_id_int}). Payer: {payer_code}")

            if payer_code != "MCB":
                record["pre_reject"] = True
                record["pre_reject_reason"] = "Payer is not Medicare Part B"
                logging.info(f"Skipping downstream API calls for {patient_id_str} (Payer is not Medicare Part B).")
            else:
                # Fetch diagnoses
                logging.info(f"Fetching diagnoses for {patient_id_str}...")
                record["diagnoses"] = make_request(f"{BASE_URL}/pcc/diagnoses", params={"patient_id": patient_id_str})

                # Fetch coverage
                logging.info(f"Fetching coverage for {patient_id_str}...")
                record["coverage"] = make_request(f"{BASE_URL}/pcc/coverage", params={"patient_id": patient_id_str})

                # Fetch notes
                logging.info(f"Fetching notes for {patient_id_str}...")
                record["notes"] = make_request(f"{BASE_URL}/pcc/notes", params={"patient_id": patient_id_int})

                # Fetch assessments
                logging.info(f"Fetching assessments for {patient_id_str}...")
                record["assessments"] = make_request(f"{BASE_URL}/pcc/assessments", params={"patient_id": patient_id_int})

            all_patient_records.append(record)

    df = pd.DataFrame(all_patient_records)
    df.to_pickle(CACHE_FILE)
    logging.info(f"Ingestion complete. Saved {len(df)} patient records to {CACHE_FILE}")

if __name__ == "__main__":
    ingest_data()
