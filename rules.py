import os
import pandas as pd
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

DATA_DIR = "data"
INPUT_FILE = os.path.join(DATA_DIR, "extracted_patient_data.pkl")
OUTPUT_FILE = os.path.join(DATA_DIR, "final_triage_output.pkl")

def apply_triage_rules(row):
    """
    Applies Medicare Part B compliance filters to determine routing decision.
    """
    # Rule 1: Payer Validation
    if row.get("pre_reject"):
        return "reject", "Patient does not possess an active Medicare Part B policy."

    # Check coverage list for active MCB (payer_code == "MCB" and effective_to is null)
    coverage_list = row.get("coverage", [])
    has_active_mcb = False
    for cov in coverage_list:
        if cov.get("payer_code") == "MCB" and cov.get("effective_to") is None:
            has_active_mcb = True
            break
            
    if not has_active_mcb:
        return "reject", "Patient does not possess an active Medicare Part B policy."

    # Rule 2: Wound Count Validation
    wounds = row.get("extracted_wounds", [])
    if not wounds:
        return "reject", "No active wound documented in notes or assessments."

    if len(wounds) > 1:
        # Check if there's exactly one primary wound flagged
        primary_wounds = [w for w in wounds if w.get("is_primary_wound")]
        if len(primary_wounds) != 1:
            return "flag_for_review", "Multiple wounds detected in documentation. Requires manual verification."
        else:
            # If there's exactly one primary wound, we evaluate that one
            wound = primary_wounds[0]
    else:
        wound = wounds[0]

    # Rule 3: Parameter Completeness
    missing_fields = []
    if wound.get("length_cm") is None:
        missing_fields.append("Length")
    if wound.get("width_cm") is None:
        missing_fields.append("Width")
    if wound.get("depth_cm") is None:
        missing_fields.append("Depth")
    if wound.get("drainage_amount") is None or str(wound.get("drainage_amount")).lower() in ["none", "null", "nan", ""]:
        # Wait, the rule says "documented drainage level (none / light / moderate / heavy)". 
        # If the drainage_amount is explicitly "none", that IS a documented level.
        # But if it's None (Python None) or missing, then it's incomplete.
        if wound.get("drainage_amount") is None:
            missing_fields.append("Drainage level")

    if missing_fields:
        fields_str = ", ".join(missing_fields)
        return "flag_for_review", f"Incomplete clinical parameters missing: {fields_str}."

    # Rule 4: Compliance Match
    wound_type = wound.get("wound_type", "Wound")
    location = wound.get("location", "specified location")
    return "auto_accept", f"Fully documented {wound_type} at {location}. All billing criteria verified."

def run_triage():
    if not os.path.exists(INPUT_FILE):
        logging.error(f"Input file {INPUT_FILE} not found. Please run extract.py first.")
        return

    logging.info(f"Loading extracted data from {INPUT_FILE}...")
    df = pd.read_pickle(INPUT_FILE)

    decisions = []
    reasons = []

    for idx, row in df.iterrows():
        decision, reason = apply_triage_rules(row)
        decisions.append(decision)
        reasons.append(reason)

    df["routing_decision"] = decisions
    df["reason"] = reasons

    df.to_pickle(OUTPUT_FILE)
    logging.info(f"Triage complete. Saved final output to {OUTPUT_FILE}")

    # Log summary counts
    counts = df["routing_decision"].value_counts()
    logging.info("Triage Summary Counts:")
    for status, count in counts.items():
        logging.info(f"  {status.upper()}: {count}")

if __name__ == "__main__":
    run_triage()
