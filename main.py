import streamlit as st
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
import time
import re

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from streamlit.components.v1 import html

# --- CONFIGURATION ---
QUESTIONNAIRE_DURATION_SECONDS = 60 
S3_SESSION_PREFIX = "sessions/" 
S3_FINAL_RESPONSE_PREFIX = "responses/" 


# --- BOTO3 S3 HELPER FUNCTIONS ---

def get_s3_client():
    """Initializes and returns a boto3 S3 client using Streamlit secrets."""
    try:
        return boto3.client(
            "s3",
            aws_access_key_id=st.secrets.aws.aws_access_key_id,
            aws_secret_access_key=st.secrets.aws.aws_secret_access_key,
            region_name=st.secrets.aws.get("aws_region")
        )
    except (NoCredentialsError, PartialCredentialsError, ClientError) as e:
        st.error(f"AWS connection error: {e}")
        return None

def get_session_key(questionnaire_id, email):
    """Creates a standardized, safe S3 key for a session file."""
    safe_email = re.sub(r'[^a-zA-Z0-9_.-@]', '_', email)
    return f"{S3_SESSION_PREFIX}{questionnaire_id}/{safe_email}.json"

def get_or_create_session(s3_client, bucket_name, questionnaire_id, participant_info):
    """Retrieves an existing session from S3 or creates a new one."""
    s3_key = get_session_key(questionnaire_id, participant_info['email'])
    try:
        response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
        session_data = json.loads(response['Body'].read().decode('utf-8'))
        return session_data
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            new_session_data = {
                "session_id": str(uuid.uuid4()),
                "questionnaire_id": questionnaire_id,
                "participant_info": participant_info,
                "status": "in-progress",
                "start_time_iso": datetime.now(timezone.utc).isoformat(),
                "last_update_iso": datetime.now(timezone.utc).isoformat(),
                "responses": {}
            }
            try:
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=s3_key,
                    Body=json.dumps(new_session_data, indent=4),
                    ContentType="application/json"
                )
                return new_session_data
            except ClientError as put_e:
                st.error(f"Failed to create new session in S3: {put_e}")
                return None
        else:
            st.error(f"Could not retrieve session from S3: {e}")
            return None

def update_session_responses(s3_client, bucket_name, session_data, new_responses):
    """Updates the responses in the S3 session file."""
    session_data["responses"] = new_responses
    session_data["last_update_iso"] = datetime.now(timezone.utc).isoformat()
    s3_key = get_session_key(session_data['questionnaire_id'], session_data['participant_info']['email'])
    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=json.dumps(session_data, indent=4),
            ContentType="application/json"
        )
        return True
    except ClientError as e:
        print(f"Background save failed: {e}")
        return False

def finalize_submission(s3_client, bucket_name, final_session_data):
    """Saves the final data and moves the session file to the responses folder."""
    final_session_data['status'] = 'completed'
    final_session_data["last_update_iso"] = datetime.now(timezone.utc).isoformat()
    
    old_key = get_session_key(final_session_data['questionnaire_id'], final_session_data['participant_info']['email'])
    new_key = old_key.replace(S3_SESSION_PREFIX, S3_FINAL_RESPONSE_PREFIX, 1)

    try:
        s3_client.put_object(
            Bucket=bucket_name, Key=new_key, Body=json.dumps(final_session_data, indent=4), ContentType="application/json"
        )
        s3_client.delete_object(Bucket=bucket_name, Key=old_key)
        return True
    except ClientError as e:
        st.error(f"Failed to finalize submission file: {e}")
        return False


# --- UI AND TIMER FUNCTIONS ---

def load_questionnaire_from_query(module_names):
    all_questions = []
    for name in module_names:
        file_path = os.path.join("questions", f"{name}.json")
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                all_questions.extend(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            st.error(f"Module '{name}.json' not found or invalid.")
            continue
    return all_questions if all_questions else None

def display_question(question_data, saved_response):
    st.subheader(question_data['question'])
    q_type = question_data.get("type", "open_ended")
    options = question_data.get("options", [])
    q_id = question_data["id"]

    if q_type == "open_ended":
        return st.text_area("Your Answer", value=saved_response or "", key=q_id, height=150)
    elif q_type == "multiple_choice":
        index = options.index(saved_response) if saved_response in options else 0
        return st.radio("Choose one:", options, index=index, key=q_id)
    elif q_type == "multi_select":
        valid_saved = [opt for opt in saved_response if isinstance(saved_response, list) and opt in options]
        return st.multiselect("Select all that apply:", options, default=valid_saved, key=q_id)

def run_timer_component(end_time):
    end_time_ms = int(end_time.timestamp() * 1000)
    js_code = f"""
    <div id="t-container" style="padding: 10px; border: 1px solid #ccc; border-radius: 5px;">
        <h4 style="margin-top: 0; margin-bottom: 5px;">Time Remaining</h4>
        <div id="t-countdown" style="font-size: 2em; font-weight: bold; color: #dc3545;">--:--</div>
    </div>
    <script>
    (function() {{
        const endTime = {end_time_ms};
        const timerEl = document.getElementById("t-countdown");
        if (!timerEl) return;
        const interval = setInterval(function() {{
            const dist = endTime - new Date().getTime();
            if (dist < 0) {{
                clearInterval(interval);
                timerEl.innerHTML = "00:00";
                // THIS IS THE FIX: Force a reload of the main page.
                window.parent.location.reload();
                return;
            }}
            const mins = Math.floor((dist % (1000 * 60 * 60)) / (1000 * 60));
            const secs = Math.floor((dist % (1000 * 60)) / 1000);
            timerEl.innerHTML = (mins<10?"0":"")+mins+":"+(secs<10?"0":"")+secs;
        }}, 1000);
    }})();
    </script>
    """
    # We no longer need the return value from this component
    html(js_code, height=100)

# --- MAIN APPLICATION ---
def main():
    st.title("On-the-Fly Questionnaire Application")

    module_names = st.query_params.get_all("q")
    if not module_names:
        st.warning("Please use a valid invitation link.")
        return

    questionnaire_id = "-".join(module_names)
    s3_client = get_s3_client()
    if not s3_client: return

    bucket_name = st.secrets.aws.s3_bucket_name
    session_data_key = f"session_data_{questionnaire_id}"

    if session_data_key not in st.session_state:
        with st.form("participant_form"):
            full_name = st.text_input("Full Name")
            email = st.text_input("Email")
            if st.form_submit_button("Start or Resume Assessment"):
                if full_name and email:
                    participant_info = {"full_name": full_name, "email": email}
                    session_data = get_or_create_session(s3_client, bucket_name, questionnaire_id, participant_info)
                    if session_data:
                        st.session_state[session_data_key] = session_data
                        st.rerun()
                else:
                    st.error("Please enter your full name and email.")
        st.stop()
    
    # --- SESSION LOADED ---
    current_session = st.session_state[session_data_key]

    # GATE 1: Check if already completed
    if current_session.get("status") == "completed":
        st.success("You have already completed this assessment. Thank you!")
        st.balloons()
        st.stop()

    # --- TIMER AND TIMEOUT LOGIC ---
    start_time = datetime.fromisoformat(current_session['start_time_iso'])
    end_time = start_time + timedelta(seconds=QUESTIONNAIRE_DURATION_SECONDS)
    
    with st.sidebar:
        run_timer_component(end_time)

    # GATE 2: This server-side check now catches reloads triggered by the JS timer
    if datetime.now(timezone.utc) >= end_time:
        if finalize_submission(s3_client, bucket_name, current_session):
            del st.session_state[session_data_key]
            st.warning("## Time has expired")
            st.info("Your assessment has been automatically submitted with your last saved answers.")
            st.info("You may now close this browser tab.")
        else:
            st.error("A critical error occurred while finalizing your submission. Please contact an administrator.")
        st.stop()

    # --- If not completed and not timed out, display the assessment ---
    questionnaire = load_questionnaire_from_query(module_names)
    if not questionnaire: st.stop()
    
    st.info(f"Participant: {current_session['participant_info']['full_name']} (Session is auto-saved)")

    live_responses = {}
    saved_responses = current_session.get("responses", {})
    for q_data in questionnaire:
        live_responses[q_data['id']] = display_question(q_data, saved_responses.get(q_data['id']))

    # AUTO-SAVE ON CHANGE
    if live_responses != saved_responses:
        if update_session_responses(s3_client, bucket_name, current_session, live_responses):
            st.session_state[session_data_key]['responses'] = live_responses
            st.rerun()

    # MANUAL FINAL SUBMISSION
    if st.button("Submit Final Answers"):
        current_session['responses'] = live_responses
        if finalize_submission(s3_client, bucket_name, current_session):
            st.session_state[session_data_key]['status'] = 'completed'
            st.rerun()

if __name__ == "__main__":
    main()