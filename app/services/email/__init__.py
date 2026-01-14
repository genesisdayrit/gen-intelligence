"""Email service module for sending emails via Gmail."""

from services.email.gmail_client import send_html_email

__all__ = ["send_html_email"]
