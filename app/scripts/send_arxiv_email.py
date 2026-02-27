#!/usr/bin/env python3
"""Send Daily ArXiv Articles Email.

Fetches random articles from random arXiv categories and sends
an HTML email summary.

Usage:
    python -m scripts.send_arxiv_email                     # Send email
    python -m scripts.send_arxiv_email --dry-run           # Generate without sending
    python -m scripts.send_arxiv_email --output email.html # Save HTML to file

Requires environment variables:
    - GMAIL_ACCOUNT, GMAIL_PASSWORD (for sending)
"""

import argparse
import logging
import random
import sys
from pathlib import Path

import feedparser
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.email.gmail_client import send_html_email

logger = logging.getLogger(__name__)

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


def get_arxiv_articles(category: str, max_results: int = 5, total_fetch: int = 100):
    """Fetch random articles from an arXiv category.

    Args:
        category: arXiv category code (e.g. "cs.AI")
        max_results: Number of articles to return
        total_fetch: Number of recent articles to sample from

    Returns:
        List of (title, url, summary) tuples
    """
    base_url = "http://export.arxiv.org/api/query?"
    query = (
        f"{base_url}search_query=cat:{category}"
        f"&max_results={total_fetch}"
        f"&sortBy=lastUpdatedDate&sortOrder=descending"
    )

    feed = feedparser.parse(query)
    entries = [
        (entry.title.replace("\n", " "), entry.link, entry.summary.replace("\n", " "))
        for entry in feed.entries
    ]

    return random.sample(entries, min(max_results, len(entries)))


def category_name_from_code(code: str) -> str:
    """Look up human-readable name for an arXiv category code."""
    for name, val in ARXIV_CATEGORIES.items():
        if val == code:
            return name
    return code


# =============================================================================
# Email Building
# =============================================================================


def build_html_email(categories_data: list[tuple[str, list]]) -> str:
    """Build HTML email body from fetched article data.

    Args:
        categories_data: List of (category_name, articles) where articles
                         is a list of (title, url, summary) tuples
    """
    sections = []
    for category_name, articles in categories_data:
        article_items = []
        for title, url, summary in articles:
            article_items.append(
                f'<li style="margin-bottom: 12px;">'
                f'<a href="{url}" style="color: #1a73e8; text-decoration: none; '
                f'font-weight: 600;">{title}</a>'
                f'<br><span style="color: #555; font-size: 13px;">{summary[:200]}...</span>'
                f"</li>"
            )

        sections.append(
            f'<div style="margin-bottom: 24px;">'
            f'<h2 style="color: #333; border-bottom: 2px solid #1a73e8; '
            f'padding-bottom: 4px;">{category_name}</h2>'
            f'<ul style="list-style: none; padding: 0;">{"".join(article_items)}</ul>'
            f"</div>"
        )

    html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8">'
        "</head><body>"
        '<div style="max-width: 700px; margin: 0; font-family: '
        "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
        'padding: 20px;">'
        '<h1 style="color: #222;">Daily ArXiv Articles</h1>'
        f'{"".join(sections)}'
        "</div></body></html>"
    )
    return html


# =============================================================================
# Main
# =============================================================================


def run_arxiv_email(
    dry_run: bool = False,
    output: str | None = None,
    categories_count: int = 3,
    articles_per_category: int = 5,
) -> bool:
    """Fetch arXiv articles and send email.

    Returns:
        True if successful, False otherwise
    """
    load_dotenv()

    random_codes = random.sample(
        list(ARXIV_CATEGORIES.values()), min(categories_count, len(ARXIV_CATEGORIES))
    )

    categories_data = []
    for code in random_codes:
        name = category_name_from_code(code)
        logger.info("Fetching articles for %s (%s)", name, code)
        articles = get_arxiv_articles(code, articles_per_category)
        if articles:
            categories_data.append((name, articles))

    if not categories_data:
        logger.error("No articles fetched from any category")
        return False

    html_content = build_html_email(categories_data)

    if output:
        Path(output).write_text(html_content)
        logger.info("HTML saved to %s", output)

    if dry_run:
        logger.info("Dry run — email not sent")
        return True

    return send_html_email("random arxiv articles", html_content)


def main():
    parser = argparse.ArgumentParser(description="Send daily arXiv articles email")
    parser.add_argument("--dry-run", action="store_true", help="Generate without sending")
    parser.add_argument("--output", type=str, help="Save HTML to file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = run_arxiv_email(dry_run=args.dry_run, output=args.output)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
