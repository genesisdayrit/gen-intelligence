#!/usr/bin/env python3
"""Send Daily Stanford Encyclopedia of Philosophy Email.

Navigates to a random entry on the Stanford Encyclopedia of Philosophy,
scrapes the content, sends it to OpenAI for summarization, and emails
the result.

Usage:
    python -m scripts.send_plato_email                    # Send email
    python -m scripts.send_plato_email --dry-run          # Generate without sending

Requires environment variables:
    - OPENAI_API_KEY (for summarization)
    - GMAIL_ACCOUNT, GMAIL_PASSWORD (for sending)
"""

import argparse
import logging
import sys
from pathlib import Path

import os
import shutil

import markdown2
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.email.gmail_client import send_html_email

logger = logging.getLogger(__name__)

PLATO_URL = "https://plato.stanford.edu/index.html"


def scrape_entry_content(url: str) -> tuple[str | None, str | None]:
    """Scrape and extract content from a Stanford Encyclopedia entry.

    Returns:
        Tuple of (full_content, title) or (None, None) on failure.
    """
    response = requests.get(url)
    if response.status_code != 200:
        logger.error("Failed to retrieve entry. Status code: %s", response.status_code)
        return None, None

    soup = BeautifulSoup(response.content, "html.parser")

    title_tag = soup.find("h1")
    title = title_tag.get_text() if title_tag else "Unknown Entry"

    main_text = soup.find("div", {"id": "main-text"})
    content = main_text.get_text(separator="\n") if main_text else ""

    full_content = f"Title: {title}\n\nContent:\n{content}"
    return full_content, title


def get_random_entry_url() -> str | None:
    """Use headless Chrome to click 'Random Entry' and return the URL."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # Debian/Docker installs chromium as 'chromium' not 'google-chrome'
    chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
    if chromium_path:
        options.binary_location = chromium_path

    chromedriver_path = shutil.which("chromedriver")
    service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()

    driver = webdriver.Chrome(options=options, service=service)
    try:
        driver.get(PLATO_URL)
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Random Entry"))
        ).click()
        WebDriverWait(driver, 10).until(EC.url_changes(PLATO_URL))
        return driver.current_url
    except Exception as e:
        logger.error("Failed to get random entry URL: %s", e)
        return None
    finally:
        driver.quit()


def summarize_with_openai(scraped_content: str) -> str:
    """Send scraped content to OpenAI for summarization."""
    api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key)

    system_prompt = (
        "You are an expert in philosophy and have been asked to provide insights "
        "on a random entry from the Stanford Encyclopedia of Philosophy."
    )

    prompt = f"""
    The following is a random entry from the Stanford Encyclopedia of Philosophy.
    Please highlight in bullet points key ideas, people, and concepts as well as the outline of the entry.
    Also give recommendations for further reading or consumption to learn more about the topic(s) covered in the entry
    as well as reflective questions that are good to have conversations about.
    Scraped Content:
    {scraped_content}
    """

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.7,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    return completion.choices[0].message.content or ""


def run_plato_email(dry_run: bool = False) -> bool:
    """Fetch a random Plato entry, summarize it, and send email.

    Returns:
        True if successful, False otherwise.
    """
    load_dotenv()

    logger.info("Getting random entry from Stanford Encyclopedia of Philosophy...")
    random_entry_url = get_random_entry_url()
    if not random_entry_url:
        return False

    logger.info("Random entry: %s", random_entry_url)

    scraped_content, entry_title = scrape_entry_content(random_entry_url)
    if not scraped_content:
        return False

    logger.info("Summarizing entry with OpenAI...")
    openai_response = summarize_with_openai(scraped_content)

    markdown_content = f"URL: {random_entry_url}\n\n{openai_response}"
    html_body = markdown2.markdown(markdown_content)

    subject = f"Plato Random Entry: {entry_title}"

    if dry_run:
        logger.info("Dry run — email not sent")
        logger.info("Subject: %s", subject)
        logger.info("Body:\n%s", openai_response)
        return True

    return send_html_email(subject, html_body)


def main():
    parser = argparse.ArgumentParser(
        description="Send daily Stanford Encyclopedia of Philosophy email"
    )
    parser.add_argument("--dry-run", action="store_true", help="Generate without sending")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = run_plato_email(dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
