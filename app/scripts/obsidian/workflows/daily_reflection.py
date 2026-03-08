#!/usr/bin/env python3
"""Daily evening reflection email with AI prompts.

Fetches the latest daily action file from the Obsidian vault via Dropbox,
generates AI reflection prompts using OpenAI, and sends a
"Daily Vision PM Check In" email.

Usage:
    python -m scripts.obsidian.workflows.daily_reflection
"""

import argparse
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
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


# ===== Content Fetching =====

def _fetch_latest_file_contents(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Fetch the latest file content from the vault root in Dropbox."""
    result = dbx.files_list_folder(vault_path)
    files = [e for e in result.entries if isinstance(e, dropbox.files.FileMetadata)]
    if not files:
        raise FileNotFoundError("No files found in the specified folder.")

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


# ===== AI Response =====

def _get_openai_reflection(text: str) -> str:
    """Get a reflection response from OpenAI."""
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not openai_api_key:
        raise EnvironmentError("OPENAI_API_KEY environment variable not set")

    client = OpenAI(api_key=openai_api_key)
    system_prompt = (
        "You are a curious, kind, and creative friend who is interested in supporting the user's vision "
        "and wanting the best for them. Imagine that the day is already over. You are skilled at asking "
        "the user smart questions and prompts that help them reflect on their day and think about what "
        "actions they took towards moving towards their vision and goals today and this week."
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

def _send_email(subject: str, extracted_text: str, ai_response: str,
                to_email: str, from_email: str, password: str):
    """Send an HTML email with today's plan and AI reflection prompts."""
    try:
        s = smtplib.SMTP(host='smtp.gmail.com', port=587)
        s.starttls()
        s.login(from_email, password)

        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = to_email
        msg['Subject'] = subject

        formatted_plan = f"<h3>Today's Plan:</h3>{extracted_text.replace(chr(10), '<br>')}"
        formatted_reflection = f"<h3>Reflection:</h3>{ai_response.replace(chr(10), '<br>')}"

        message_body = f"{formatted_plan}<br><hr><br>{formatted_reflection}"
        msg.attach(MIMEText(message_body, 'html'))

        s.send_message(msg)
        s.quit()
        logger.info("Email sent successfully")
    except Exception as e:
        logger.error(f"Error occurred while sending email: {e}")
        raise


# ===== Main =====

def daily_reflection() -> bool:
    """Run the daily evening reflection workflow.

    Fetches the latest daily action content, generates AI reflection prompts,
    and sends an email.

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

        # Fetch latest file content
        latest_file_contents = _fetch_latest_file_contents(dbx, dropbox_vault_path)
        extracted_text = _extract_vision_section(latest_file_contents)

        # Get AI reflection response
        ai_response = _get_openai_reflection(extracted_text)

        # Send email
        system_tz = pytz.timezone(timezone_str)
        current_date = datetime.now(system_tz).strftime("%m/%d/%Y")

        _send_email(
            subject=f"Daily Vision PM Check In ({current_date})",
            extracted_text=extracted_text,
            ai_response=ai_response,
            to_email=to_email,
            from_email=from_email,
            password=password
        )
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except ValueError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily evening reflection email with AI prompts")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = daily_reflection()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
