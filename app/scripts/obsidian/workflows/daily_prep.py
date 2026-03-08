#!/usr/bin/env python3
"""Daily morning prep email with AI coaching.

Fetches the latest daily action file and weekly map from the Obsidian vault
via Dropbox, generates an AI coaching response using OpenAI, and sends a
"Daily Vision AM Check-In" email with all three sections.

Usage:
    python -m scripts.obsidian.workflows.daily_prep
"""

import argparse
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

logger = logging.getLogger(__name__)

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")


def _refresh_access_token() -> str:
    """Refresh the Dropbox access token using the refresh token."""
    client_id = os.getenv('DROPBOX_ACCESS_KEY')
    client_secret = os.getenv('DROPBOX_ACCESS_SECRET')
    refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError("Missing Dropbox credentials in .env file")

    response = requests.post(
        'https://api.dropbox.com/oauth2/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client_id,
            'client_secret': client_secret
        }
    )

    if response.status_code == 200:
        data = response.json()
        access_token = data.get('access_token')
        expires_in = data.get('expires_in')
        redis_client.set('DROPBOX_ACCESS_TOKEN', access_token, ex=expires_in)
        return access_token
    else:
        raise EnvironmentError(f"Failed to refresh token: {response.status_code}")


def _get_dropbox_client() -> dropbox.Dropbox:
    """Get authenticated Dropbox client."""
    access_token = redis_client.get('DROPBOX_ACCESS_TOKEN')
    if not access_token:
        access_token = _refresh_access_token()
    return dropbox.Dropbox(access_token)


# ===== Dropbox Folder Helpers =====

def _find_folder_by_suffix(dbx: dropbox.Dropbox, parent_path: str, suffix: str) -> str:
    """Find a subfolder ending with the given suffix."""
    result = dbx.files_list_folder(parent_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith(suffix):
            return entry.path_lower
    raise FileNotFoundError(f"Could not find a folder ending with '{suffix}' in {parent_path}")


# ===== Content Fetching =====

def _fetch_latest_daily_action(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Fetch the latest daily action file contents from Dropbox."""
    daily_folder = _find_folder_by_suffix(dbx, vault_path, "_Daily")
    action_folder = _find_folder_by_suffix(dbx, daily_folder, "_Daily-Action")

    result = dbx.files_list_folder(action_folder)
    files = [e for e in result.entries if isinstance(e, dropbox.files.FileMetadata)]
    if not files:
        raise FileNotFoundError("No files found in the '_Daily-Action' folder.")

    latest_file = max(files, key=lambda x: x.client_modified)
    _, response = dbx.files_download(latest_file.path_lower)
    return response.content.decode('utf-8')


def _extract_vision_section(file_contents: str) -> str:
    """Extract the section starting with 'Vision Objective 1:' and ending with '---'."""
    pattern = r"(Vision Objective 1:.*?---)"
    match = re.search(pattern, file_contents, re.DOTALL)
    if match:
        return match.group(1).strip()
    raise ValueError("The expected Vision Objective section could not be found in the file.")


def _find_weekly_map(dbx: dropbox.Dropbox, vault_path: str) -> str | None:
    """Locate and extract the content of this week's weekly map."""
    try:
        weekly_folder = _find_folder_by_suffix(dbx, vault_path, "_Weekly")
        maps_folder = _find_folder_by_suffix(dbx, weekly_folder, "_Weekly-Maps")

        system_tz = pytz.timezone(timezone_str)
        today = datetime.now(system_tz)
        days_until_sunday = (6 - today.weekday()) % 7
        next_sunday = today + timedelta(days=days_until_sunday)
        sunday_str = next_sunday.strftime("%Y-%m-%d").lower()

        result = dbx.files_list_folder(maps_folder)
        files = [e for e in result.entries if isinstance(e, dropbox.files.FileMetadata)]

        for f in files:
            if f"weekly map {sunday_str}" in f.name.lower():
                _, response = dbx.files_download(f.path_lower)
                return response.content.decode('utf-8')

        logger.warning("Could not find this week's map.")
        return None
    except FileNotFoundError:
        logger.warning("Weekly map not found. Proceeding without it.")
        return None


# ===== AI Response =====

def _get_openai_response(text: str) -> str:
    """Get a coaching response from OpenAI."""
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not openai_api_key:
        raise EnvironmentError("OPENAI_API_KEY environment variable not set")

    client = OpenAI(api_key=openai_api_key)
    system_prompt = (
        "You are a curious, kind, and creative friend who is interested in supporting the user's vision "
        "and wanting the best for them. You are skilled at making smart suggestions that will help the user "
        "meet their own goals and desires."
    )

    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )
    return completion.choices[0].message.content


# ===== Email =====

def _send_email(subject: str, daily_prep: str, todays_plan: str, weekly_map: str,
                to_email: str, from_email: str, password: str):
    """Send an HTML email with the daily prep, today's plan, and weekly map."""
    try:
        s = smtplib.SMTP(host='smtp.gmail.com', port=587)
        s.starttls()
        s.login(from_email, password)

        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = to_email
        msg['Subject'] = subject

        daily_prep_html = f"<h3>Daily Prep:</h3>{daily_prep.replace(chr(10), '<br>')}"
        todays_plan_html = f"<h3>Today's Plan:</h3>{todays_plan.replace(chr(10), '<br>')}"
        weekly_map_html = f"<h3>Weekly Map:</h3>{weekly_map.replace(chr(10), '<br>')}"

        message_body = f"{daily_prep_html}<br><hr><br>{todays_plan_html}<br><hr><br>{weekly_map_html}"
        msg.attach(MIMEText(message_body, 'html'))

        s.send_message(msg)
        s.quit()
        logger.info("Email sent successfully")
    except Exception as e:
        logger.error(f"Error occurred while sending email: {e}")
        raise


# ===== Main =====

def daily_prep() -> bool:
    """Run the daily morning prep workflow.

    Fetches daily action content and weekly map, generates AI coaching,
    and sends an email with all sections.

    Returns:
        True on success, False on error.
    """
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    from_email = os.getenv('GMAIL_ACCOUNT')
    password = os.getenv('GMAIL_PASSWORD')
    to_email = from_email

    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False
    if not from_email or not password:
        logger.error("GMAIL_ACCOUNT or GMAIL_PASSWORD environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()

        # Fetch daily content
        latest_file_contents = _fetch_latest_daily_action(dbx, dropbox_vault_path)
        extracted_text = _extract_vision_section(latest_file_contents)

        # Fetch weekly map
        weekly_map_content = _find_weekly_map(dbx, dropbox_vault_path)
        if weekly_map_content is None:
            weekly_map_content = "Weekly map not available."

        # Get AI response
        ai_response = _get_openai_response(extracted_text)

        # Send email
        system_tz = pytz.timezone(timezone_str)
        current_date = datetime.now(system_tz).strftime("%m/%d/%Y")

        _send_email(
            subject=f"Daily Vision AM Check-In ({current_date})",
            daily_prep=ai_response,
            todays_plan=extracted_text,
            weekly_map=weekly_map_content,
            to_email=to_email,
            from_email=from_email,
            password=password
        )
        return True

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily morning prep email with AI coaching")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = daily_prep()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
