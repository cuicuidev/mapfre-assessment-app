import streamlit as st
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
import re
import random

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from streamlit.components.v1 import html

# --- CONFIGURATION ---
QUESTIONNAIRE_DURATION_SECONDS = 3600
S3_SESSION_PREFIX = "sessions/"
S3_INDEX_PREFIX = "session_index/"
S3_FINAL_RESPONSE_PREFIX = "responses/" 


# --- BOTO3 S3 HELPER FUNCTIONS ---
def get_s3_client():
    """Initializes a boto3 S3 client."""
    try:
        return boto3.client(
            "s3",
            aws_access_key_id=st.secrets.aws.aws_access_key_id,
            aws_secret_access_key=st.secrets.aws.aws_secret_access_key,
            region_name=st.secrets.aws.get("aws_region")
        )
    except (NoCredentialsError, PartialCredentialsError, ClientError) as e:
        st.error(f"AWS connection error: {e}"); return None

def check_if_already_completed(s3_client, bucket, questionnaire_id, email):
    """Scans the final responses to see if this email has already submitted."""
    prefix = f"{S3_FINAL_RESPONSE_PREFIX}{questionnaire_id}/"
    paginator = s3_client.get_paginator('list_objects_v2')
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if "Contents" not in page:
                continue
            for obj in page['Contents']:
                if obj['Size'] > 0:
                    response_obj = s3_client.get_object(Bucket=bucket, Key=obj['Key'])
                    data = json.loads(response_obj['Body'].read().decode('utf-8'))
                    if data.get("participant_info", {}).get("email") == email:
                        return True # Found a match
        return False
    except ClientError:
        st.warning("Could not verify previous submissions due to an S3 error.")
        return False # Fail open but warn the user/admin

def get_session_by_id(s3_client, bucket, questionnaire_id, session_id):
    """Loads a session directly using its unique ID."""
    s3_key = f"{S3_SESSION_PREFIX}{questionnaire_id}/{session_id}.json"
    try:
        response = s3_client.get_object(Bucket=bucket, Key=s3_key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except ClientError:
        return None

def get_or_create_session_by_email(s3_client, bucket, questionnaire_id, participant_info):
    """Finds or creates a persistent session, returning the session data."""
    safe_email = re.sub(r'[^a-zA-Z0-9_.-@]', '_', participant_info['email'])
    index_key = f"{S3_INDEX_PREFIX}{questionnaire_id}/{safe_email}.json"
    
    try:
        response = s3_client.get_object(Bucket=bucket, Key=index_key)
        index_data = json.loads(response['Body'].read().decode('utf-8'))
        session_id = index_data['session_id']
        return get_session_by_id(s3_client, bucket, questionnaire_id, session_id)
    except ClientError as e:
        if e.response['Error']['Code'] != 'NoSuchKey':
            st.error(f"Error checking for existing session: {e}"); return None
        
        new_session_id = str(uuid.uuid4())
        new_session_data = {
            "session_id": new_session_id, "questionnaire_id": questionnaire_id,
            "participant_info": participant_info, "status": "in-progress",
            "start_time_iso": None, 
            "last_update_iso": datetime.now(timezone.utc).isoformat(), "responses": {}
        }
        session_key = f"{S3_SESSION_PREFIX}{questionnaire_id}/{new_session_id}.json"
        try:
            s3_client.put_object(Bucket=bucket, Key=session_key, Body=json.dumps(new_session_data, indent=4))
            s3_client.put_object(Bucket=bucket, Key=index_key, Body=json.dumps({"session_id": new_session_id}))
            return new_session_data
        except ClientError as put_e:
            st.error(f"Failed to create new session in S3: {put_e}"); return None

def save_session(s3_client, bucket, session_data):
    """Generic function to save the current state of a session to S3."""
    session_data["last_update_iso"] = datetime.now(timezone.utc).isoformat()
    s3_key = f"{S3_SESSION_PREFIX}{session_data['questionnaire_id']}/{session_data['session_id']}.json"
    try:
        s3_client.put_object(Bucket=bucket, Key=s3_key, Body=json.dumps(session_data, indent=4))
        return True
    except ClientError: return False

def finalize_submission(s3_client, bucket, session_data):
    """Saves the final data and cleans up session/index files."""
    session_data['status'] = 'completed'
    session_data["last_update_iso"] = datetime.now(timezone.utc).isoformat()
    session_id, q_id = session_data['session_id'], session_data['questionnaire_id']
    
    final_key = f"{S3_FINAL_RESPONSE_PREFIX}{q_id}/{session_id}.json"
    session_key = f"{S3_SESSION_PREFIX}{q_id}/{session_id}.json"
    safe_email = re.sub(r'[^a-zA-Z0-9_.-@]', '_', session_data['participant_info']['email'])
    index_key = f"{S3_INDEX_PREFIX}{q_id}/{safe_email}.json"

    try:
        s3_client.put_object(Bucket=bucket, Key=final_key, Body=json.dumps(session_data, indent=4))
        s3_client.delete_object(Bucket=bucket, Key=session_key)
        # Keep the index file to prevent starting a new assessment
        return True
    except ClientError as e:
        st.error(f"Failed to finalize submission: {e}"); return False

# --- UI AND TIMER FUNCTIONS ---
def load_questionnaire_from_query(module_names):
    all_questions = []
    for name in module_names:
        file_path = os.path.join("questions", f"{name}.json")
        try:
            with open(file_path, 'r', encoding='utf-8') as f: all_questions.extend(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError): continue
    return all_questions if all_questions else None

def display_question(q_data, saved_response):
    st.subheader(q_data['question'])
    q_type = q_data.get("type", "open_ended")
    opts = q_data.get("options", [])
    q_id = q_data["id"]

    if q_type == "open_ended": return st.text_area("Your Answer", value=saved_response or "", key=q_id, height=150)
    if q_type == "multi_select": return st.multiselect("Select:", opts, default=[o for o in saved_response if o in opts] if isinstance(saved_response, list) else [], key=q_id)
    if q_type == "multi_select_pills":return st.pills("Select:", opts, default=[o for o in saved_response if o in opts] if isinstance(saved_response, list) else [], key=q_id, selection_mode="multi")
    if q_type == "multiple_choice":
        response = st.selectbox(
            "Choose one:",
            options=["NS/NC"] + opts
        )

        return response if response != "NS/NC" else None

def run_timer_component(end_time):
    end_time_ms = int(end_time.timestamp() * 1000)
    js_code = f"""<script>
    (function() {{
        const timerEl = window.parent.document.querySelector("#assessment-timer-countdown");
        if (!timerEl) return;
        const interval = setInterval(function() {{
            const dist = {end_time_ms} - new Date().getTime();
            if (dist < 0) {{
                clearInterval(interval);
                timerEl.innerHTML = "00:00";
                window.parent.location.reload(); return;
            }}
            const mins = Math.floor((dist % 3600000) / 60000);
            const secs = Math.floor((dist % 60000) / 1000);
            timerEl.innerHTML = (mins<10?"0":"")+mins+":"+(secs<10?"0":"")+secs;
        }}, 1000);
    }})();</script>"""
    st.sidebar.markdown("""<div style="padding: 10px; border: 1px solid #ccc; border-radius: 5px;">
        <h4 style="margin-top: 0; margin-bottom: 5px;">Time Remaining</h4>
        <div id="assessment-timer-countdown" style="font-size: 2em; font-weight: bold; color: #dc3545;">--:--</div>
    </div>""", unsafe_allow_html=True)
    html(js_code, height=0, width=0)

def display_completion_screen():
    """Shows the final 'Thank You' message after successful submission."""
    st.success("## Thank you for your submission!")
    st.info("Your responses have been successfully recorded. You may now close this browser tab.")
    st.balloons()

# --- MAIN APPLICATION ---
def main():
    st.title("On-the-Fly Questionnaire Application")

    if 'submission_complete' not in st.session_state:
        st.session_state.submission_complete = False

    if st.session_state.submission_complete:
        display_completion_screen()
        st.stop()

    module_names = st.query_params.get_all("q")
    if not module_names: st.warning("Please use a valid invitation link."); return
    questionnaire_id = "-".join(module_names)
    
    s3_client = get_s3_client();
    if not s3_client: return
    bucket_name = st.secrets.s3.bucket_name

    session_id_from_url = st.query_params.get("session_id")
    current_session = None

    if session_id_from_url:
        current_session = get_session_by_id(s3_client, bucket_name, questionnaire_id, session_id_from_url)

    if not current_session:
        with st.form("participant_form"):
            full_name = st.text_input("Full Name")
            email = st.text_input("Email")
            if st.form_submit_button("Start or Resume Assessment"):
                if full_name and email:
                    # --- COMPLETION CHECK ---
                    if check_if_already_completed(s3_client, bucket_name, questionnaire_id, email):
                        st.error("This email address has already completed this assessment.")
                    else:
                        p_info = {"full_name": full_name, "email": email}
                        session_data = get_or_create_session_by_email(s3_client, bucket_name, questionnaire_id, p_info)
                        if session_data:
                            q_params = "&".join([f"q={m}" for m in module_names])
                            session_url = f"/?{q_params}&session_id={session_data['session_id']}"
                            st.markdown(f'<a href="{session_url}" target="_self" style="display:inline-block;padding:10px 16px;font-size:18px;font-weight:bold;color:white;background-color:#007bff;border-radius:5px;text-decoration:none;">Click Here to Begin Assessment</a>', unsafe_allow_html=True)
                else: st.error("Please enter your full name and email.")
        st.stop()
    
    st.session_state['session_data'] = current_session

    if current_session.get("start_time_iso") is None:
        current_session["start_time_iso"] = datetime.now(timezone.utc).isoformat()
        if save_session(s3_client, bucket_name, current_session):
            st.session_state['session_data'] = current_session
        else:
            st.error("Could not activate session. Please try again."); st.stop()

    if current_session.get("status") == "completed":
        st.session_state.submission_complete = True
        st.rerun()

    start_time = datetime.fromisoformat(current_session['start_time_iso'])
    end_time = start_time + timedelta(seconds=QUESTIONNAIRE_DURATION_SECONDS)
    if datetime.now(timezone.utc) >= end_time:
        if finalize_submission(s3_client, bucket_name, current_session):
            st.session_state.submission_complete = True
            st.rerun()
        else:
            st.error("A critical error occurred while finalizing your submission.")
        st.stop()
        
    run_timer_component(end_time)

    questionnaire = load_questionnaire_from_query(module_names)
    if not questionnaire: st.stop()
    st.info(f"Participant: {current_session['participant_info']['full_name']} (Progress is auto-saved)")

    live_responses, saved_responses = {}, current_session.get("responses", {})
    for q_data in questionnaire:
        live_responses[q_data['id']] = display_question(q_data, saved_responses.get(q_data['id']))

    if live_responses != saved_responses:
        current_session['responses'] = live_responses
        if save_session(s3_client, bucket_name, current_session):
            st.session_state['session_data']['responses'] = live_responses; st.rerun()

    if st.button("Submit Final Answers"):
        current_session['responses'] = live_responses
        if finalize_submission(s3_client, bucket_name, current_session):
            st.session_state.submission_complete = True
            if 'session_data' in st.session_state: del st.session_state['session_data']
            st.query_params.clear()
            st.rerun()

if __name__ == "__main__":
    main()