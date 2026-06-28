import os
import json
import pandas as pd
import logging
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

DATA_DIR = "data"
INPUT_FILE = os.path.join(DATA_DIR, "raw_patient_data.pkl")
OUTPUT_FILE = os.path.join(DATA_DIR, "extracted_patient_data.pkl")

# Pydantic schemas for structured extraction
class WoundProfile(BaseModel):
    wound_type: str = Field(description="Type of the wound (e.g., pressure ulcer, diabetic foot ulcer, venous stasis ulcer, arterial ulcer, surgical site infection, abscess, burn, etc.)")
    stage: Optional[str] = Field(None, description="Stage of the pressure ulcer (stages 2-4, unstageable). Null if not a pressure ulcer.")
    location: str = Field(description="Anatomical location of the wound (e.g., Sacrum, Left Heel, Right Lateral Malleolus)")
    length_cm: Optional[float] = Field(None, description="Length of the wound in cm")
    width_cm: Optional[float] = Field(None, description="Width of the wound in cm")
    depth_cm: Optional[float] = Field(None, description="Depth of the wound in cm")
    drainage_amount: Optional[str] = Field(None, description="Drainage level: none, light, moderate, heavy")
    is_primary_wound: bool = Field(description="True if this is the primary wound described. If multiple, identify the primary based on clinical severity or explicit labeling.")

class WoundPayload(BaseModel):
    wounds: List[WoundProfile]

def get_gemini_client():
    """Initializes the Gemini client using the environment variable."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)

def extract_from_assessment(assessments) -> Optional[List[dict]]:
    """
    Parses the latest complete assessment's raw_json.
    Supports:
    1. Flat structure (typical mock API example)
    2. Sectioned structure with "Wound narrative" (Style B)
    3. Sectioned structure with individual questions (Style A)
    """
    if not assessments:
        return None
    
    # Sort assessments by assessment_date descending to get the latest
    valid_assessments = sorted(
        assessments, 
        key=lambda x: x.get("assessment_date") or "", 
        reverse=True
    )
    
    if not valid_assessments:
        return None
        
    latest = valid_assessments[0]
    raw_json_str = latest.get("raw_json")
    if not raw_json_str:
        return None
        
    try:
        data = json.loads(raw_json_str)
        
        # Check if it has a flat structure (e.g. wound_type is a top-level key)
        if "wound_type" in data:
            stage_val = data.get("stage")
            if stage_val is not None:
                stage_val = str(stage_val)
            return [{
                "wound_type": data.get("wound_type", "Unknown"),
                "stage": stage_val,
                "location": data.get("location", "Unknown"),
                "length_cm": data.get("length_cm"),
                "width_cm": data.get("width_cm"),
                "depth_cm": data.get("depth_cm"),
                "drainage_amount": data.get("drainage_amount"),
                "is_primary_wound": True
            }]
            
        # Check if it has a sectioned structure
        if "sections" in data:
            # Flatten questions
            qa = {}
            for section in data.get("sections", []):
                for q in section.get("questions", []):
                    question_text = q.get("question", "").strip()
                    answer_text = q.get("answer")
                    if answer_text is not None:
                        qa[question_text] = str(answer_text).strip()
            
            # 1. Style B: Wound narrative
            if "Wound narrative" in qa:
                narrative = qa["Wound narrative"]
                import re
                parts = [p.strip() for p in narrative.split('/')]
                wound_type = "Unknown"
                location = "Unknown"
                length_cm = None
                width_cm = None
                depth_cm = None
                stage = None
                drainage_amount = None

                for part in parts:
                    if " to " in part:
                        subparts = part.split(" to ", 1)
                        wound_type = subparts[0].strip()
                        location = subparts[1].strip()
                    elif "Measures" in part:
                        # Extract dimensions (e.g. "Measures 2.9 cm x 2.8 cm" or "Measures 4.2x3.1x1.5cm")
                        # Find all numbers (including floats)
                        nums = re.findall(r"\d+\.?\d*", part)
                        if len(nums) >= 2:
                            length_cm = float(nums[0])
                            width_cm = float(nums[1])
                        if len(nums) >= 3:
                            depth_cm = float(nums[2])
                    elif "Stage:" in part:
                        stage_str = part.split("Stage:", 1)[1].strip()
                        if stage_str.lower() != "n/a":
                            stage_match = re.search(r"\d+|unstageable", stage_str.lower())
                            if stage_match:
                                stage = stage_match.group(0)
                            else:
                                stage = stage_str
                    elif "Drainage:" in part:
                        drainage_str = part.split("Drainage:", 1)[1].strip()
                        if "," in drainage_str:
                            drainage_amount = drainage_str.split(",", 1)[1].strip().lower()
                        else:
                            drainage_amount = drainage_str.lower()

                return [{
                    "wound_type": wound_type,
                    "stage": stage,
                    "location": location,
                    "length_cm": length_cm,
                    "width_cm": width_cm,
                    "depth_cm": depth_cm,
                    "drainage_amount": drainage_amount,
                    "is_primary_wound": True
                }]
                
            # 2. Style A: Structured sections
            wound_type = qa.get("Wound Type", "Unknown")
            stage = qa.get("Stage")
            if stage and stage.lower() == "n/a":
                stage = None
                
            location = qa.get("Location", "Unknown")
            laterality = qa.get("Laterality")
            if laterality and laterality.lower() != "n/a" and laterality.lower() != "unknown":
                if laterality.lower() not in location.lower():
                    location = f"{laterality} {location}"
                    
            length_cm = None
            width_cm = None
            depth_cm = None
            
            if "Length (cm)" in qa:
                try:
                    length_cm = float(qa["Length (cm)"])
                except ValueError:
                    pass
            if "Width (cm)" in qa:
                try:
                    width_cm = float(qa["Width (cm)"])
                except ValueError:
                    pass
            if "Depth (cm)" in qa:
                try:
                    depth_cm = float(qa["Depth (cm)"])
                except ValueError:
                    pass
                    
            drainage_amount = qa.get("Drainage Amount")
            if drainage_amount:
                drainage_amount = drainage_amount.lower()
                if drainage_amount == "n/a":
                    drainage_amount = None
            
            return [{
                "wound_type": wound_type,
                "stage": stage,
                "location": location,
                "length_cm": length_cm,
                "width_cm": width_cm,
                "depth_cm": depth_cm,
                "drainage_amount": drainage_amount,
                "is_primary_wound": True
            }]
            
        return None
    except Exception as e:
        logging.error(f"Error parsing assessment JSON: {e}")
        return None

def extract_from_note_with_llm(client, note_text: str) -> List[dict]:
    """
    Calls the Gemini API to extract structured wound data from the progress note.
    """
    prompt = f"""
    You are a clinical NLP system. Analyze the following progress note and extract all wound profiles.
    Identify the primary wound if multiple wounds are described.
    Ensure all measurements (length, width, depth) are extracted as numbers in cm.
    If shorthand is used (e.g. 'Meas 4.2x3.1x1.5cm'), split it into length (4.2), width (3.1), and depth (1.5).
    If a dimension is not present, set it to null.
    For drainage_amount, map to one of: 'none', 'light', 'moderate', 'heavy'.
    For pressure ulcer stages, map to '2', '3', '4', or 'unstageable'.

    Progress Note:
    \"\"\"
    {note_text}
    \"\"\"
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=WoundPayload,
                temperature=0.0,
            ),
        )
        
        result = json.loads(response.text)
        return result.get("wounds", [])
    except APIError as e:
        logging.error(f"Gemini API Error: {e}")
        raise e
    except Exception as e:
        logging.error(f"Failed to parse Gemini response: {e}")
        return []

def run_extraction():
    if not os.path.exists(INPUT_FILE):
        logging.error(f"Input file {INPUT_FILE} not found. Please run ingest.py first.")
        return

    logging.info(f"Loading raw data from {INPUT_FILE}...")
    df = pd.read_pickle(INPUT_FILE)
    
    # Initialize client
    try:
        client = get_gemini_client()
    except Exception as e:
        logging.error(e)
        logging.warning("Proceeding, but LLM extraction will fail if needed. Please ensure GEMINI_API_KEY is set.")
        client = None

    extracted_wounds = []
    source_used_list = []

    for idx, row in df.iterrows():
        patient_id = row["patient_id"]
        pre_reject = row["pre_reject"]

        if pre_reject:
            extracted_wounds.append([])
            source_used_list.append("Skipped (Pre-rejected)")
            continue

        logging.info(f"Extracting wound data for patient {patient_id}...")
        
        # 1. Try Assessment
        wounds = extract_from_assessment(row.get("assessments"))
        if wounds:
            extracted_wounds.append(wounds)
            source_used_list.append("Assessment (Structured)")
            logging.info(f"Successfully extracted from structured assessment for {patient_id}.")
            continue

        # 2. Try Notes
        notes = row.get("notes")
        if notes:
            # Sort notes by effective_date descending to get the latest
            valid_notes = sorted(
                notes,
                key=lambda x: x.get("effective_date") or "",
                reverse=True
            )
            if valid_notes:
                latest_note = valid_notes[0]
                note_text = latest_note.get("note_text")
                if note_text and client:
                    try:
                        wounds = extract_from_note_with_llm(client, note_text)
                        extracted_wounds.append(wounds)
                        source_used_list.append("Progress Note (Gemini Extracted)")
                        logging.info(f"Successfully extracted from note using Gemini for {patient_id}. Found {len(wounds)} wounds.")
                        continue
                    except Exception as e:
                        logging.error(f"Error during LLM extraction for {patient_id}: {e}")
                elif note_text:
                    logging.warning(f"No Gemini client available. Cannot extract from note for {patient_id}.")
        
        # 3. Fallback: No data
        extracted_wounds.append([])
        source_used_list.append("None")
        logging.warning(f"No wound data could be extracted for patient {patient_id}.")

    df["extracted_wounds"] = extracted_wounds
    df["source_used"] = source_used_list

    df.to_pickle(OUTPUT_FILE)
    logging.info(f"Extraction complete. Saved data to {OUTPUT_FILE}")

if __name__ == "__main__":
    run_extraction()
