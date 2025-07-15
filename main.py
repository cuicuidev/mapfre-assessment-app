import streamlit as st
import json
import os
import uuid
from datetime import datetime

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError

# --- LOADING FUNCTIONS ---

def get_s3_client():
    """Initializes and returns a boto3 S3 client using Streamlit secrets."""
    try:
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=st.secrets.aws.aws_access_key_id,
            aws_secret_access_key=st.secrets.aws.aws_secret_access_key,
            region_name=st.secrets.aws.get("aws_region") # .get() is safer
        )
        return s3_client
    except (NoCredentialsError, PartialCredentialsError):
        st.error("AWS credentials not found. Please configure your secrets.toml file.")
        return None
    except Exception as e:
        st.error(f"An error occurred while connecting to AWS: {e}")
        return None

def load_question_module(module_name):
    """Loads a single question module from the 'questions' folder."""
    file_path = os.path.join("questions", f"{module_name}.json")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        st.error(f"Error: Questionnaire module '{module_name}.json' not found.")
        return []
    except json.JSONDecodeError:
        st.error(f"Error: The file '{module_name}.json' is not a valid JSON.")
        return []

def load_questionnaire_from_query(module_names):
    """Loads and concatenates questions from a list of module names."""
    all_questions = []
    for module_name in module_names:
        questions = load_question_module(module_name)
        if not questions: # Stop if any module fails to load
            return None
        all_questions.extend(questions)
    return all_questions

# --- RESPONSE HANDLING FUNCTIONS ---


def has_submitted_to_s3(email, questionnaire_id):
    """Checks the S3 bucket to see if a participant has already submitted."""
    s3_client = get_s3_client()
    if not s3_client:
        st.error("Cannot check for previous submissions due to AWS connection issue.")
        return True # Fail safely by blocking submission

    bucket_name = st.secrets.aws.s3_bucket_name
    prefix = "responses/"
    
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)
        for page in pages:
            # Check if the page contains any objects
            if "Contents" not in page:
                continue

            for obj in page['Contents']:
                # --- FIX STARTS HERE ---
                # 1. Skip any object that is empty (like a folder placeholder)
                if obj['Size'] == 0:
                    continue
                
                # 2. Add a try-except block as a safety net for any other non-JSON files
                try:
                    response_obj = s3_client.get_object(Bucket=bucket_name, Key=obj['Key'])
                    data = json.loads(response_obj['Body'].read().decode('utf-8'))
                    
                    if (data.get("participant_info", {}).get("email") == email and
                        data.get("questionnaire_id") == questionnaire_id):
                        return True # Found a match
                except (json.JSONDecodeError, ClientError) as e:
                    # Log the error for debugging but don't crash the app
                    st.warning(f"Skipping a non-JSON or inaccessible object in S3: {obj['Key']}. Error: {e}")
                    continue
                # --- FIX ENDS HERE ---

        return False # No matching submission found after checking all objects
    
    except ClientError as e:
        # This handles errors with the listing operation itself
        st.error(f"Error listing S3 objects: {e}. Assuming no prior submission.")
        return False # Allow user to proceed if the check fails
    
def save_response_to_s3(questionnaire_id, participant_info, responses):
    """Constructs the response JSON and uploads it to the configured S3 bucket."""
    s3_client = get_s3_client()
    if not s3_client:
        st.error("Could not save response due to AWS connection issue.")
        return # Stop if client fails

    submission_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()

    response_data = {
        "submission_id": submission_id,
        "questionnaire_id": questionnaire_id,
        "participant_info": participant_info,
        "responses": responses,
        "submitted_at": timestamp
    }
    
    # Convert dict to JSON string
    json_body = json.dumps(response_data, indent=4, ensure_ascii=False)
    
    bucket_name = st.secrets.aws.s3_bucket_name
    # Define a structured key (like a file path)
    s3_key = f"responses/{questionnaire_id}/response_{submission_id}.json"

    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=json_body,
            ContentType="application/json"
        )
        return True # Indicate success
    except ClientError as e:
        st.error(f"An error occurred while saving to S3: {e}")
        return False # Indicate failure

# --- DISPLAY FUNCTION ---

def display_question(question_data):
    """Displays a single question and returns the response."""
    st.subheader(question_data['question'])
    q_type = question_data.get("type", "open_ended")
    options = question_data.get("options", [])
    question_id = question_data["id"]

    # Use the question_id as the key to ensure uniqueness across modules
    if q_type == "open_ended":
        return st.text_area("Your Answer", key=question_id, height=150)
    elif q_type == "multiple_choice":
        return st.radio("Choose one:", options, key=question_id)
    elif q_type == "multi_select":
        return st.multiselect("Select all that apply:", options, key=question_id)
    return None

# --- MAIN APPLICATION ---

def main():
    st.title("On-the-Fly Questionnaire Application")

    # Get all values for the 'q' parameter, preserving order
    module_names = st.query_params.get_all("q")

    if not module_names:
        st.warning("Please use an invitation link that specifies the questionnaire modules (e.g., ?q=module1&q=module2).")
        return

    # Create a unique, order-dependent ID for this combination of questionnaires
    questionnaire_id = "-".join(module_names)

    # Load the full questionnaire from the modules in the URL
    questionnaire = load_questionnaire_from_query(module_names)
    if not questionnaire:
        st.stop()

    st.header("Participant Information")
    
    # Session state key is now unique to the specific questionnaire combination
    session_key = f"participant_info_{questionnaire_id}"

    if session_key not in st.session_state:
        with st.form("participant_form"):
            full_name = st.text_input("Full Name")
            email = st.text_input("Email")
            submitted_info = st.form_submit_button("Start Questionnaire")

            if submitted_info:
                if full_name and email:
                    st.session_state[session_key] = {"full_name": full_name, "email": email}
                    st.rerun()
                else:
                    st.error("Please enter your full name and email.")
    else:
        participant_info = st.session_state[session_key]
        st.info(f"Participant: {participant_info['full_name']} ({participant_info['email']})")

        if has_submitted_to_s3(participant_info['email'], questionnaire_id):
            st.error("You have already completed this questionnaire. Thank you for your participation!")
            st.stop()

        # Create a user-friendly title from the module names
        title = " & ".join([name.replace('_', ' ').title() for name in module_names])
        st.header(f"Questionnaire: {title}")
        st.markdown("---")
        
        with st.form("questionnaire_form"):
            responses = {}
            for q_data in questionnaire:
                response = display_question(q_data)
                responses[q_data['id']] = response

            submitted = st.form_submit_button("Submit Responses")

            if submitted:
                save_response_to_s3(questionnaire_id, participant_info, responses)
                st.success("Your responses have been successfully submitted! Thank you.")
                st.balloons()
                del st.session_state[session_key]
                st.stop()

if __name__ == "__main__":
    main()