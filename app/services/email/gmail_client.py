"""Gmail SMTP client for sending HTML emails.

Usage:
    from services.email.gmail_client import send_html_email

    success = send_html_email(
        subject="Weekly Summary",
        html_body="<h1>Hello</h1>",
    )

Requires environment variables:
    - GMAIL_ACCOUNT: Gmail email address
    - GMAIL_PASSWORD: Gmail app password (not regular password)
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def get_gmail_credentials() -> tuple[str, str]:
    """Get Gmail credentials from environment variables.

    Returns:
        Tuple of (email, password)

    Raises:
        ValueError: If credentials are not set
    """
    email = os.getenv("GMAIL_ACCOUNT")
    password = os.getenv("GMAIL_PASSWORD")

    if not email:
        raise ValueError("GMAIL_ACCOUNT environment variable not set")
    if not password:
        raise ValueError("GMAIL_PASSWORD environment variable not set")

    return email, password


def send_html_email(
    subject: str,
    html_body: str,
    to_email: str | None = None,
) -> bool:
    """Send an HTML email via Gmail SMTP.

    Args:
        subject: Email subject line
        html_body: HTML content for email body
        to_email: Recipient email (defaults to sender email for self-send)

    Returns:
        True if email sent successfully, False otherwise
    """
    try:
        from_email, password = get_gmail_credentials()
        recipient = to_email or from_email

        logger.debug(f"Preparing email to {recipient}")

        # Build the email message
        msg = MIMEMultipart("alternative")
        msg["From"] = from_email
        msg["To"] = recipient
        msg["Subject"] = subject

        # Attach HTML content
        html_part = MIMEText(html_body, "html")
        msg.attach(html_part)

        # Connect and send
        logger.debug(f"Connecting to {SMTP_SERVER}:{SMTP_PORT}")

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(from_email, password)
            server.send_message(msg)

        logger.info(f"Email sent successfully to {recipient}")
        return True

    except ValueError as e:
        logger.error(f"Credential error: {e}")
        return False
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"Gmail authentication failed: {e}")
        logger.error("Ensure you're using an App Password, not your regular password")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False
