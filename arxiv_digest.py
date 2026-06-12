"""
arXiv Daily Digest Bot

Fetches recent papers from specified arXiv categories, selects the 10 most
relevant per category via the KIT LLM API, and delivers a digest by email.

Author: Aritra Bal (ETP)
Date: 2026-06-12
"""

import json
import os
import smtplib
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText

import httpx
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CATEGORIES: list[str] = ["hep-ph", "hep-ex", "cs.LG", "quant-ph"]
FETCH_N: int = 50

RESEARCHER_PROFILE: str = (
    "The reader is a particle physicist and ML researcher working on "
    "Lorentz-equivariant neural networks, jet tagging, quantum machine learning "
    "(continuous-variable QML, qumode architectures), anomaly detection at CMS, "
    "simulation-based inference, and information geometry (Fisher information, "
    "natural gradient). Prioritise papers directly relevant to these topics."
)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = "http://www.w3.org/2005/Atom"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kit_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["KIT_LLM_KEY"],
        base_url="https://ki-toolbox.scc.kit.edu/api",
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def fetch_papers(category: str, n: int) -> list[dict]:
    """Fetch the n most recently submitted papers from an arXiv category.

    Returns a list of dicts with keys: ``title``, ``abstract``, ``id``.
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
            papers.append(
                {
                    "title": title_el.text.strip(),
                    "abstract": abstract_el.text.strip(),
                    "id": id_el.text.strip(),
                }
            )
        except (AttributeError, TypeError):
            continue

    return papers


def select_best(papers: list[dict], category: str) -> list[dict]:
    """Use the KIT LLM to select the 10 most relevant papers for the researcher.

    Sends a numbered list of titles + truncated abstracts and expects a JSON
    array of integer indices back.  Raises on parse failure or wrong type.
    Silently drops out-of-range indices.
    """
    numbered = "\n\n".join(
        f"[{i}] {p['title']}\n{p['abstract'][:400]}"
        for i, p in enumerate(papers)
    )

    system_prompt = (
        f"{RESEARCHER_PROFILE}\n\n"
        "You are selecting papers for the researcher's daily digest. "
        "Return ONLY a JSON array of exactly 10 integer indices (0-based) "
        "identifying the most relevant papers. No explanation, no markdown, "
        "just the raw JSON array, e.g. [3, 7, 12, ...]."
    )
    user_prompt = (
        f"Category: {category}\n\n"
        f"Papers:\n{numbered}\n\n"
        "Return the 10 best indices as a JSON array."
    )

    client = _kit_client()
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


def build_email_body(results: dict[str, list[dict]]) -> str:
    """Format all selected papers into a plain-text email body."""
    sections: list[str] = []
    for category, papers in results.items():
        lines = [f"=== {category} ===", ""]
        for idx, paper in enumerate(papers, start=1):
            arxiv_url = paper["id"]
            lines.append(f"{idx}. {paper['title']}")
            lines.append(f"   {arxiv_url}")
            lines.append("")
        sections.append("\n".join(lines))
    return "\n".join(sections)


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
    """Entry point: fetch, select, and email the daily arXiv digest."""
    results: dict[str, list[dict]] = {}
    for category in CATEGORIES:
        papers = fetch_papers(category, FETCH_N)
        selected = select_best(papers, category)
        results[category] = selected

    body = build_email_body(results)
    send_email("[arXiv] Daily Digest", body)
    print("Digest sent successfully.")


if __name__ == "__main__":
    main()
