#!/usr/bin/env python3
"""Send Daily ArXiv Articles Email.

Fetches random articles from random arXiv categories and sends
a plain text email with titles and URLs.

Usage:
    python -m scripts.send_arxiv_email                    # Send email
    python -m scripts.send_arxiv_email --dry-run          # Generate without sending
    python -m scripts.send_arxiv_email --output email.txt # Save to file

Requires environment variables:
    - GMAIL_ACCOUNT, GMAIL_PASSWORD (for sending)
"""

import argparse
import logging
import random
import sys
import threading
import time
from pathlib import Path

import feedparser
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.email.gmail_client import send_html_email

logger = logging.getLogger(__name__)
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.2
ARXIV_429_COOLDOWN_SECONDS = 6.0
_ARXIV_REQUEST_LOCK = threading.Lock()
_LAST_ARXIV_REQUEST_TS = 0.0


def _wait_for_arxiv_slot(min_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS) -> None:
    """Enforce a minimum delay between arXiv API requests across this process."""
    global _LAST_ARXIV_REQUEST_TS

    with _ARXIV_REQUEST_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_ARXIV_REQUEST_TS
        if elapsed < min_interval_seconds:
            time.sleep(min_interval_seconds - elapsed)
        _LAST_ARXIV_REQUEST_TS = time.monotonic()

# =============================================================================
# ArXiv Categories
# =============================================================================

ARXIV_CATEGORIES = {
    # Physics
    "Astrophysics": "astroph",
    "Condensed Matter": "cond-mat",
    "General Relativity and Quantum Cosmology": "gr-qc",
    "High Energy Physics - Experiment": "hep-ex",
    "High Energy Physics - Lattice": "hep-lat",
    "High Energy Physics - Phenomenology": "hep-ph",
    "High Energy Physics - Theory": "hep-th",
    "Mathematical Physics": "math-ph",
    "Nonlinear Sciences": "nlin",
    "Nuclear Experiment": "nucl-ex",
    "Nuclear Theory": "nucl-th",
    "Physics": "physics",
    "Quantum Physics": "quant-ph",
    # Mathematics
    "Algebraic Geometry": "math.AG",
    "Algebraic Topology": "math.AT",
    "Analysis of PDEs": "math.AP",
    "Category Theory": "math.CT",
    "Classical Analysis and ODEs": "math.CA",
    "Combinatorics": "math.CO",
    "Commutative Algebra": "math.AC",
    "Complex Variables": "math.CV",
    "Differential Geometry": "math.DG",
    "Dynamical Systems": "math.DS",
    "Functional Analysis": "math.FA",
    "General Mathematics": "math.GM",
    "General Topology": "math.GN",
    "Geometric Topology": "math.GT",
    "Group Theory": "math.GR",
    "History and Overview": "math.HO",
    "Information Theory (Math)": "math.IT",
    "K-Theory and Homology": "math.KT",
    "Logic (Math)": "math.LO",
    "Mathematical Physics (Math)": "math.MP",
    "Metric Geometry": "math.MG",
    "Number Theory": "math.NT",
    "Numerical Analysis (Math)": "math.NA",
    "Operator Algebras": "math.OA",
    "Optimization and Control": "math.OC",
    "Probability": "math.PR",
    "Quantum Algebra": "math.QA",
    "Representation Theory": "math.RT",
    "Rings and Algebras": "math.RA",
    "Spectral Theory": "math.SP",
    "Statistics Theory": "math.ST",
    "Symplectic Geometry": "math.SG",
    # Computer Science
    "Architecture (CS)": "cs.AR",
    "Artificial Intelligence": "cs.AI",
    "Computation and Language": "cs.CL",
    "Computational Engineering, Finance, and Science": "cs.CE",
    "Computational Geometry": "cs.CG",
    "Computer Science and Game Theory": "cs.GT",
    "Computer Vision and Pattern Recognition": "cs.CV",
    "Computers and Society": "cs.CY",
    "Cryptography and Security": "cs.CR",
    "Data Structures and Algorithms": "cs.DS",
    "Databases": "cs.DB",
    "Digital Libraries": "cs.DL",
    "Discrete Mathematics (CS)": "cs.DM",
    "Distributed, Parallel, and Cluster Computing": "cs.DC",
    "Emerging Technologies": "cs.ET",
    "Formal Languages and Automata Theory": "cs.FL",
    "General Literature (CS)": "cs.GL",
    "Graphics": "cs.GR",
    "Human-Computer Interaction": "cs.HC",
    "Information Retrieval": "cs.IR",
    "Information Theory (CS)": "cs.IT",
    "Machine Learning (CS)": "cs.LG",
    "Logic in Computer Science": "cs.LO",
    "Mathematical Software": "cs.MS",
    "Multiagent Systems": "cs.MA",
    "Multimedia": "cs.MM",
    "Networking and Internet Architecture": "cs.NI",
    "Neural and Evolutionary Computing": "cs.NE",
    "Numerical Analysis (CS)": "cs.NA",
    "Operating Systems": "cs.OS",
    "Other Computer Science": "cs.OH",
    "Performance": "cs.PF",
    "Programming Languages": "cs.PL",
    "Robotics": "cs.RO",
    "Social Informatics": "cs.SI",
    "Software Engineering": "cs.SE",
    "Sound": "cs.SD",
    "Symbolic Computation": "cs.SC",
    "Systems and Control": "cs.SY",
    # Quantitative Biology
    "Biomolecules": "q-bio.BM",
    "Cell Behavior": "q-bio.CB",
    "Genomics": "q-bio.GN",
    # Quantitative Finance
    "Computational Finance": "q-fin.CP",
    "General Finance": "q-fin.GN",
    "Mathematical Finance": "q-fin.MF",
    "Portfolio Management": "q-fin.PM",
    "Pricing of Securities": "q-fin.PR",
    "Risk Management": "q-fin.RM",
    "Statistical Finance": "q-fin.ST",
    "Trading and Market Microstructure": "q-fin.TR",
    # Statistics
    "Applications (Stats)": "stat.AP",
    "Computation (Stats)": "stat.CO",
    "Machine Learning (Stats)": "stat.ML",
    "Methodology": "stat.ME",
    "Statistics Theory (Stats)": "stat.TH",
    # Economics
    "Econometrics": "econ.EM",
    "General Economics": "econ.GN",
    "Theoretical Economics": "econ.TH",
}


# =============================================================================
# Data Fetching
# =============================================================================


def get_arxiv_articles(
    category: str,
    max_results: int = 5,
    total_fetch: int = 100,
    max_retries: int = 3,
    retry_backoff_seconds: float = 1.5,
    min_request_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS,
):
    """Fetch random articles from an arXiv category.

    Args:
        category: arXiv category code (e.g. "cs.AI")
        max_results: Number of articles to return
        total_fetch: Number of recent articles to sample from

    Returns:
        List of (title, url) tuples
    """
    base_url = "https://export.arxiv.org/api/query?"
    query = (
        f"{base_url}search_query=cat:{category}"
        f"&max_results={total_fetch}"
        f"&sortBy=lastUpdatedDate&sortOrder=descending"
    )

    for attempt in range(1, max_retries + 1):
        _wait_for_arxiv_slot(min_request_interval_seconds)
        feed = feedparser.parse(query, request_headers={"User-Agent": "gen-intelligence-arxiv-email/1.0"})
        status = getattr(feed, "status", None)

        if status in RETRYABLE_HTTP_STATUSES:
            logger.warning(
                "arXiv request failed for %s with HTTP %s (attempt %d/%d)",
                category,
                status,
                attempt,
                max_retries,
            )
            if attempt < max_retries:
                if status == 429:
                    time.sleep(max(retry_backoff_seconds * attempt, ARXIV_429_COOLDOWN_SECONDS))
                else:
                    time.sleep(retry_backoff_seconds * attempt)
                continue
            logger.warning("Exhausted retries for %s after HTTP %s", category, status)
            return []

        if status and status >= 400:
            logger.error("arXiv request failed for %s with non-retryable HTTP %s", category, status)
            return []

        entries = []
        for entry in feed.entries:
            pdf_link = entry.link.replace("/abs/", "/pdf/")
            html_link = entry.link.replace("/abs/", "/html/")
            entries.append((entry.title, entry.link, pdf_link, html_link))

        if not entries:
            if getattr(feed, "bozo", False):
                logger.warning(
                    "arXiv feed parse warning for %s: %s",
                    category,
                    getattr(feed, "bozo_exception", "unknown parse error"),
                )
            return []

        return random.sample(entries, min(max_results, len(entries)))

    return []


def category_name_from_code(code: str) -> str:
    """Look up human-readable name for an arXiv category code."""
    for name, val in ARXIV_CATEGORIES.items():
        if val == code:
            return name
    return code


# =============================================================================
# Email Building
# =============================================================================


def build_plain_text_email(categories_data: list[tuple[str, list]]) -> str:
    """Build plain text email body from fetched article data.

    Args:
        categories_data: List of (category_name, articles) where articles
                         is a list of (title, url) tuples
    """
    email_body = ""
    for category_name, articles in categories_data:
        email_body += f"Articles from category: {category_name}\n\n"
        for title, url, pdf_url, html_url in articles:
            email_body += f"Title: {title}\nURL: {url}\nPDF: {pdf_url}\nHTML: {html_url}\n\n"
        email_body += "-" * 50 + "\n"

    return email_body


# =============================================================================
# Main
# =============================================================================


def run_arxiv_email(
    dry_run: bool = False,
    output: str | None = None,
    categories_count: int = 3,
    articles_per_category: int = 5,
    category_fetch_retries: int = 3,
    max_category_attempts: int | None = None,
    min_request_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS,
) -> bool:
    """Fetch arXiv articles and send email.

    Returns:
        True if successful, False otherwise
    """
    load_dotenv()

    target_categories = min(categories_count, len(ARXIV_CATEGORIES))
    candidate_codes = list(ARXIV_CATEGORIES.values())
    random.shuffle(candidate_codes)
    # Keep a hard cap so prolonged API throttling cannot stall a run for too long.
    effective_max_category_attempts = max_category_attempts or min(
        len(candidate_codes), max(target_categories * 4, target_categories)
    )

    categories_data = []
    categories_tried = 0
    for code in candidate_codes:
        if categories_tried >= effective_max_category_attempts:
            logger.warning(
                "Hit max category attempt limit (%d) before filling target categories",
                effective_max_category_attempts,
            )
            break
        if len(categories_data) >= target_categories:
            break

        categories_tried += 1
        name = category_name_from_code(code)
        logger.info("Fetching articles for %s (%s)", name, code)
        articles = get_arxiv_articles(
            code,
            articles_per_category,
            max_retries=category_fetch_retries,
            min_request_interval_seconds=min_request_interval_seconds,
        )
        if articles:
            categories_data.append((name, articles))
        else:
            logger.warning("No articles returned for %s (%s), trying another category", name, code)

    if len(categories_data) < target_categories:
        logger.warning(
            "Requested %d category(ies) but only found %d with articles",
            target_categories,
            len(categories_data),
        )

    if not categories_data:
        logger.error("No articles fetched from any category")
        return False

    email_body = build_plain_text_email(categories_data)

    if output:
        Path(output).write_text(email_body)
        logger.info("Email body saved to %s", output)

    if dry_run:
        logger.info("Dry run — email not sent")
        return True

    return send_html_email("Random Arxiv Articles", email_body.replace("\n", "<br>"))


def main():
    parser = argparse.ArgumentParser(description="Send daily arXiv articles email")
    parser.add_argument("--dry-run", action="store_true", help="Generate without sending")
    parser.add_argument("--output", type=str, help="Save email body to file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = run_arxiv_email(dry_run=args.dry_run, output=args.output)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
