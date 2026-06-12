"""
arXiv Daily Digest Bot

Fetches recent papers from specified arXiv categories, selects and ranks the
most relevant ones per category via the KIT LLM API, then uses a second LLM
call to craft a personalised digest email. All user-facing settings are read
from config.yaml.

Author: Aritra Bal (ETP)
Date: 2026-06-12
"""

import json
import os
import smtplib
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from pathlib import Path

import httpx
import yaml
from openai import OpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

with (Path(__file__).parent / "config.yaml").open() as _f:
    _cfg = yaml.safe_load(_f)

CATEGORIES: list[str] = _cfg["categories"]
FETCH_N: int = _cfg["fetch_n"]
SELECT_N: int = _cfg["select_n"]
RESEARCHER_PROFILE: str = _cfg["researcher_profile"].strip()
OUTPUT_INSTRUCTIONS: str = _cfg["output_instructions"].strip()
USER_NAME: str = _cfg["user"]["name"]
LLM_BASE_URL: str = _cfg["llm"]["base_url"]
EMAIL_SUBJECT: str = _cfg["email"]["subject"]

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = "http://www.w3.org/2005/Atom"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["KIT_LLM_KEY"],
        base_url=LLM_BASE_URL,
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def fetch_papers(category: str, n: int) -> list[dict]:
    """Fetch the n most recently submitted papers from an arXiv category.

    Returns a list of dicts with keys: ``title``, ``authors``, ``abstract``, ``id``.
    Raises ``httpx.HTTPStatusError`` on non-200 responses.
    Silently skips malformed XML entries.
    """
    params = {
        "search_query": f"cat:{category}",
        "max_results": n,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    response = httpx.get(ARXIV_API_URL, params=params, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    papers: list[dict] = []
    for entry in root.findall(f"{{{ARXIV_NS}}}entry"):
        try:
            title_el = entry.find(f"{{{ARXIV_NS}}}title")
            abstract_el = entry.find(f"{{{ARXIV_NS}}}summary")
            id_el = entry.find(f"{{{ARXIV_NS}}}id")
            if title_el is None or abstract_el is None or id_el is None:
                continue
            authors = [
                a.findtext(f"{{{ARXIV_NS}}}name", "").strip()
                for a in entry.findall(f"{{{ARXIV_NS}}}author")
            ]
            papers.append(
                {
                    "title": title_el.text.strip(),
                    "authors": authors,
                    "abstract": abstract_el.text.strip(),
                    "id": id_el.text.strip(),
                }
            )
        except (AttributeError, TypeError):
            continue

    return papers


def select_best(papers: list[dict], category: str, n: int = SELECT_N) -> list[dict]:
    """Use the LLM to select and rank the n most relevant papers.

    Sends a numbered list of titles + truncated abstracts and expects a JSON
    array of integer indices back, ordered from most to least relevant.
    Raises on parse failure or wrong type. Silently drops out-of-range indices.
    """
    numbered = "\n\n".join(
        f"[{i}] {p['title']}\nAuthors: {', '.join(p['authors'])}\n{p['abstract']}"
        for i, p in enumerate(papers)
    )

    system_prompt = (
        f"{RESEARCHER_PROFILE}\n\n"
        "You are selecting papers for a physics/ML researcher's daily digest. "
        f"Return ONLY a JSON array of exactly {n} integer indices (0-based), "
        "ordered from most to least relevant to the researcher's interests. "
        "No explanation, no markdown — just the raw JSON array, e.g. [3, 7, 12, ...]."
    )
    user_prompt = (
        f"Category: {category}\n\n"
        f"Papers:\n{numbered}\n\n"
        f"Return the {n} best indices as a JSON array, ranked most to least relevant."
    )

    client = _llm_client()
    model = os.environ["KIT_LLM_MODEL"]

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=256,
    )

    raw = completion.choices[0].message.content.strip()

    try:
        indices = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned non-JSON for category '{category}': {raw!r}"
        ) from exc

    if not isinstance(indices, list):
        raise ValueError(
            f"LLM response is not a list for category '{category}': {raw!r}"
        )

    valid = [i for i in indices if isinstance(i, int) and 0 <= i < len(papers)]
    return [papers[i] for i in valid]


def format_digest(results: dict[str, list[dict]], name: str) -> str:
    """Use the LLM to write the full personalised digest email body.

    Papers are presented with their full abstracts so the model can write
    meaningful summaries for the top picks.
    """
    sections: list[str] = []
    for category, papers in results.items():
        block = [f"Category: {category}"]
        for rank, paper in enumerate(papers, start=1):
            block.append(
                f"  [{rank}] {paper['title']}\n"
                f"      Authors: {', '.join(paper['authors'])}\n"
                f"      URL: {paper['id']}\n"
                f"      Abstract: {paper['abstract']}"
            )
        sections.append("\n".join(block))

    papers_text = "\n\n".join(sections)

    system_prompt = (
        f"{RESEARCHER_PROFILE}\n\n"
        "You are writing a personalised daily arXiv digest email for this researcher.\n\n"
        f"Formatting instructions:\n{OUTPUT_INSTRUCTIONS}"
    )
    user_prompt = (
        f"Recipient name: {name}\n\n"
        "Here are today's selected papers, already ranked from most to least "
        "relevant within each category:\n\n"
        f"{papers_text}\n\n"
        "Write the digest email body now."
    )

    client = _llm_client()
    model = os.environ["KIT_LLM_MODEL"]

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    return completion.choices[0].message.content.strip()


def send_email(subject: str, body: str) -> None:
    """Send the digest email via Gmail SMTP over SSL (port 465).

    Both sender and recipient are the configured Gmail address (self-send).
    SMTP exceptions propagate so GitHub Actions marks the run as failed.
    """
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = address

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        smtp.sendmail(address, [address], msg.as_string())


def main() -> None:
    """Entry point: fetch, select, format, and email the daily arXiv digest."""
    results: dict[str, list[dict]] = {}
    for category in CATEGORIES:
        papers = fetch_papers(category, FETCH_N)
        selected = select_best(papers, category)
        results[category] = selected

    body = format_digest(results, USER_NAME)
    send_email(EMAIL_SUBJECT, body)
    print("Digest sent successfully.")


if __name__ == "__main__":
    main()
