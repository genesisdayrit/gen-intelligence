#!/usr/bin/env python3
"""Send essay ideas from yesterday's journal entry.

Fetches yesterday's Obsidian journal note from Dropbox, asks OpenAI for 3-5
essay ideas plus supporting materials, and emails the results using the
existing Gmail helper.

Usage:
    python -m scripts.send_essay_ideas_from_journal
    python -m scripts.send_essay_ideas_from_journal --dry-run
    python -m scripts.send_essay_ideas_from_journal --output essay_ideas.html
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import dropbox
import markdown2
from dotenv import load_dotenv
from openai import OpenAI

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import SYSTEM_TZ
from scripts.linear.sync_utils import get_dropbox_client
from services.email.gmail_client import send_html_email

load_dotenv()

logger = logging.getLogger(__name__)

ESSAY_IDEAS_MODEL = "gpt-4o"
SUPPORTING_MATERIALS_MODEL = "gpt-4o-search-preview"

ESSAY_IDEAS_SYSTEM_PROMPT = (
    "You are a thoughtful and creative writer who generates insightful essay ideas "
    "based on the content provided. Focus on drawing themes, patterns, and unique angles "
    "from the provided text to create compelling essay topics. For each essay idea, "
    "provide a brief explanation of why it would be interesting to explore."
)

SUPPORTING_MATERIALS_PROMPT_TEMPLATE = """Based on the following journal entry and essay ideas, please search the web for relevant supporting materials including recent articles, essays, books, and other resources that would help develop these topics further.

JOURNAL ENTRY:
{journal_text}

ESSAY IDEAS:
{essay_ideas}

Please search for and provide:
1. Recent articles or essays from reputable publications that relate to these themes
2. Relevant books (both recent and classic) that would provide deeper insight
3. Academic papers or research that supports these topics
4. Any other valuable resources (documentaries, podcasts, etc.)

For each resource, please include:
- **Title and author/source**
- Brief description of how it relates to the journal themes and essay ideas
- Key insights or perspectives it offers
- Publication date when available

Important: Please provide proper citations and web sources for all recommendations. Use web search to find current, relevant materials rather than relying on training data. Format your response using markdown with **bold** for titles and proper [link text](url) formatting where applicable.

Focus on finding high-quality, credible sources that would genuinely help develop the essay ideas further."""


def _iter_folder_entries(dbx: dropbox.Dropbox, folder_path: str) -> list[Any]:
    """Return all entries from a Dropbox folder, following pagination."""
    result = dbx.files_list_folder(folder_path)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries


def find_folder_by_suffix(dbx: dropbox.Dropbox, parent_path: str, suffix: str) -> str:
    """Find a direct child folder whose name ends with the provided suffix."""
    for entry in _iter_folder_entries(dbx, parent_path):
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith(suffix):
            return entry.path_lower
    raise FileNotFoundError(f"Could not find a folder ending with '{suffix}' in {parent_path}")


def format_obsidian_journal_filename(target_date: date) -> str:
    """Return the lowercase journal filename used in the Obsidian vault."""
    return f"{target_date.strftime('%b')} {target_date.day}, {target_date.year}.md".lower()


def _ensure_system_timezone(value: datetime) -> datetime:
    """Treat naive datetimes as SYSTEM_TZ-local and convert aware ones into SYSTEM_TZ."""
    if value.tzinfo is None:
        return SYSTEM_TZ.localize(value)
    return value.astimezone(SYSTEM_TZ)


def fetch_yesterdays_journal_entry(
    dbx: dropbox.Dropbox,
    vault_path: str,
    now: datetime | None = None,
) -> tuple[date, str]:
    """Fetch yesterday's journal entry text using the current vault folder convention."""
    daily_folder = find_folder_by_suffix(dbx, vault_path, "_Daily")
    journal_folder = find_folder_by_suffix(dbx, daily_folder, "_Journal")

    now_local = _ensure_system_timezone(now) if now else datetime.now(SYSTEM_TZ)
    journal_date = (now_local - timedelta(days=1)).date()
    target_filename = format_obsidian_journal_filename(journal_date)

    for entry in _iter_folder_entries(dbx, journal_folder):
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        if entry.name.strip().lower() != target_filename:
            continue

        _, response = dbx.files_download(entry.path_lower)
        return journal_date, response.content.decode("utf-8")

    raise FileNotFoundError(
        f"Yesterday's journal entry ({target_filename}) was not found in {journal_folder}"
    )


def _get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY environment variable not set")
    return OpenAI(api_key=api_key)


def _coerce_message_content(content: Any) -> str:
    """Extract plain text from OpenAI message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, dict):
                    value = text.get("value") or text.get("text")
                    if value:
                        pieces.append(str(value))
                elif text:
                    pieces.append(str(text))
            else:
                text = getattr(part, "text", None)
                if text is None:
                    continue
                value = getattr(text, "value", None) or getattr(text, "text", None) or text
                if value:
                    pieces.append(str(value))
        return "\n".join(pieces).strip()
    return "" if content is None else str(content)


def generate_essay_ideas(client: OpenAI, journal_text: str) -> str:
    """Generate 3-5 essay ideas using the original GPT-4o prompt."""
    user_prompt = (
        f"Here is today's journal entry:\n\n{journal_text}\n\n"
        "Please suggest 3-5 essay ideas with brief explanations of why each would be worth exploring."
    )
    completion = client.chat.completions.create(
        model=ESSAY_IDEAS_MODEL,
        messages=[
            {"role": "system", "content": ESSAY_IDEAS_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return _coerce_message_content(completion.choices[0].message.content)


def _annotation_field(obj: Any, field: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def extract_citations(annotations: list[Any] | None) -> list[dict[str, str | None]]:
    """Normalize OpenAI search annotations into title/url pairs."""
    normalized: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()

    for annotation in annotations or []:
        candidate = annotation
        if _annotation_field(annotation, "type") == "url_citation":
            candidate = _annotation_field(annotation, "url_citation") or annotation

        title = _annotation_field(candidate, "title") or _annotation_field(annotation, "title")
        url = _annotation_field(candidate, "url") or _annotation_field(annotation, "url")
        key = (title, url)

        if key in seen or (not title and not url):
            continue
        seen.add(key)
        normalized.append(
            {
                "title": str(title or url or "Source"),
                "url": str(url) if url else None,
            }
        )

    return normalized


def get_supporting_materials_with_web_search(
    client: OpenAI,
    journal_text: str,
    essay_ideas: str,
) -> tuple[str, list[dict[str, str | None]]]:
    """Search the web for supporting materials tied to the journal and ideas."""
    completion = client.chat.completions.create(
        model=SUPPORTING_MATERIALS_MODEL,
        web_search_options={"search_context_size": "medium"},
        messages=[
            {
                "role": "user",
                "content": SUPPORTING_MATERIALS_PROMPT_TEMPLATE.format(
                    journal_text=journal_text,
                    essay_ideas=essay_ideas,
                ),
            }
        ],
    )
    message = completion.choices[0].message
    content = _coerce_message_content(message.content)
    citations = extract_citations(getattr(message, "annotations", None))
    logger.info("Web search completed successfully with %d citation(s)", len(citations))
    return content, citations


def _format_human_date(target_date: date) -> str:
    return f"{target_date.strftime('%b')} {target_date.day}, {target_date.year}"


def build_html_email(
    journal_date: date,
    essay_ideas: str,
    supporting_materials: str,
    citations: list[dict[str, str | None]],
    generated_at: datetime,
) -> str:
    """Build the email HTML body."""
    generated_local = _ensure_system_timezone(generated_at)
    essay_ideas_html = markdown2.markdown(essay_ideas, extras=["break-on-newline"])
    supporting_materials_html = markdown2.markdown(
        supporting_materials,
        extras=["break-on-newline"],
    )

    citations_section = ""
    if citations:
        citation_items = []
        for citation in citations:
            title = html.escape(citation["title"] or "Source")
            url = citation.get("url")
            if url:
                citation_items.append(
                    f"<li><a href='{html.escape(url, quote=True)}'>{title}</a></li>"
                )
            else:
                citation_items.append(f"<li>{title}</li>")
        citations_section = (
            "<section>"
            "<h2>Sources</h2>"
            "<ul>"
            f"{''.join(citation_items)}"
            "</ul>"
            "</section>"
        )

    return "\n".join(
        [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            "<meta charset='utf-8'>",
            "<style>",
            "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; line-height: 1.6; max-width: 860px; margin: 0 auto; padding: 24px; }",
            "h1 { margin-bottom: 0.25rem; }",
            "h2 { margin-top: 2rem; border-bottom: 1px solid #e5e7eb; padding-bottom: 0.35rem; }",
            ".meta { color: #6b7280; margin-top: 0; }",
            ".section { margin-top: 2rem; }",
            "a { color: #2563eb; }",
            "blockquote { color: #4b5563; border-left: 3px solid #d1d5db; margin-left: 0; padding-left: 1rem; }",
            "code { background: #f3f4f6; padding: 0.1rem 0.25rem; border-radius: 0.25rem; }",
            "</style>",
            "</head>",
            "<body>",
            "<h1>Essay Ideas From Journal</h1>",
            (
                "<p class='meta'>"
                f"Based on journal entry from {_format_human_date(journal_date)}. "
                f"Generated {html.escape(generated_local.strftime('%b %d, %Y %I:%M %p %Z'))}."
                "</p>"
            ),
            "<section class='section'>",
            "<h2>Essay Ideas</h2>",
            essay_ideas_html,
            "</section>",
            "<section class='section'>",
            "<h2>Supporting Materials</h2>",
            supporting_materials_html,
            "</section>",
            citations_section,
            "</body>",
            "</html>",
        ]
    )


def run_essay_ideas_from_journal(
    dry_run: bool = False,
    output: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Fetch yesterday's journal entry, generate ideas, and send the email."""
    load_dotenv()

    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    generated_at = _ensure_system_timezone(now) if now else datetime.now(SYSTEM_TZ)

    try:
        dbx = get_dropbox_client()
        journal_date, journal_text = fetch_yesterdays_journal_entry(dbx, vault_path, now=generated_at)
        logger.info("Fetched journal entry for %s", _format_human_date(journal_date))

        client = _get_openai_client()
        essay_ideas = generate_essay_ideas(client, journal_text)
        logger.info("Generated essay ideas")

        supporting_materials, citations = get_supporting_materials_with_web_search(
            client,
            journal_text,
            essay_ideas,
        )
        logger.info("Generated supporting materials")

        html_body = build_html_email(
            journal_date=journal_date,
            essay_ideas=essay_ideas,
            supporting_materials=supporting_materials,
            citations=citations,
            generated_at=generated_at,
        )

        if output:
            output_path = Path(output)
            output_path.write_text(html_body, encoding="utf-8")
            logger.info("Saved essay ideas HTML to %s", output_path)

        if dry_run:
            logger.info("Dry run completed; essay ideas email not sent.")
            return True

        subject = f"Essay Ideas & Supporting Materials ({generated_at.strftime('%m/%d/%Y')})"
        sent = send_html_email(subject, html_body)
        if sent:
            logger.info("Essay ideas email sent successfully.")
        else:
            logger.error("Failed to send essay ideas email.")
        return sent
    except Exception:
        logger.exception("Essay ideas from journal workflow failed")
        return False


def main():
    parser = argparse.ArgumentParser(description="Send essay ideas from yesterday's journal entry")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the email but do not send it",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Optional path to write generated HTML output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    success = run_essay_ideas_from_journal(
        dry_run=args.dry_run,
        output=args.output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
