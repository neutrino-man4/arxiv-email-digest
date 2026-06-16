"""
arXiv Daily Digest Bot

Fetches papers from specified arXiv categories for the most recent announcement
window, selects and ranks the most relevant ones per category via the KIT LLM
API, then uses a second LLM call to craft a personalised digest. Delivery is
either via Gmail SMTP or a Mattermost webhook, controlled by the `delivery` key
in config.yaml. All user-facing settings are read from config.yaml.

Author: Aritra Bal (ETP)
Date: 2026-06-12
"""

import argparse
import csv
import json
import os
import re
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import markdown as md
import yaml
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def _load_prices(path: Path) -> dict[str, tuple[float, float]]:
    """Load model prices from a CSV with columns: model, usd_per_mtok_in, usd_per_mtok_out.

    Returns a dict mapping model name → (price_in, price_out) per million tokens.
    Returns an empty dict if the file does not exist.
    """
    if not path.exists():
        return {}
    with path.open() as f:
        return {
            row["model"]: (float(row["usd_per_mtok_in"]), float(row["usd_per_mtok_out"]))
            for row in csv.DictReader(f)
        }


# Config globals — populated by main() after the --config argument is parsed.
CATEGORIES: list[str] = []
SELECT_N: int = 0
MAX_RESULTS: int = 0
RESEARCHER_PROFILE: str = ""
OUTPUT_INSTRUCTIONS: str = ""
LLM_BASE_URL: str = ""
LLM_MODEL: str = ""
DELIVERY: str = ""
EMAIL_SUBJECT: str = ""
EMAIL_TO: str = ""
EMAIL_DISPLAY_NAME: str = ""

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = "http://www.w3.org/2005/Atom"

_ET = ZoneInfo("America/New_York")
_CUTOFF_HOUR = 14       # arXiv submission cutoff: 14:00 ET on announcement days
_ANNOUNCEMENT_HOUR = 20  # arXiv announces new papers at ~20:00 ET

# (start_offset, end_offset) in days relative to today, keyed by weekday
# (Monday=0 … Sunday=6), for the most recent arXiv announcement window.
#
# arXiv announcement schedule (all times US Eastern):
#   Mon 14:00 – Tue 14:00  → announced Tue 20:00
#   Tue 14:00 – Wed 14:00  → announced Wed 20:00
#   Wed 14:00 – Thu 14:00  → announced Thu 20:00
#   Thu 14:00 – Fri 14:00  → announced Sun 20:00  (no Fri/Sat announcements)
#   Fri 14:00 – Mon 14:00  → announced Mon 20:00  (3-day weekend window)
#
# Before today's 20:00 ET announcement the relevant window is from last night.
_PRE_ANNOUNCEMENT_OFFSETS: dict[int, tuple[int, int]] = {
    0: (-4, -3),  # Monday    → Thu 14:00 – Fri 14:00  (Sun was last announcement)
    1: (-4, -1),  # Tuesday   → Fri 14:00 – Mon 14:00  (Mon was last, 3-day)
    2: (-2, -1),  # Wednesday → Mon 14:00 – Tue 14:00
    3: (-2, -1),  # Thursday  → Tue 14:00 – Wed 14:00
    4: (-2, -1),  # Friday    → Wed 14:00 – Thu 14:00  (no Fri announcement)
    6: (-4, -3),  # Sunday    → Wed 14:00 – Thu 14:00  (Thu was last announcement)
}

# At or after 20:00 ET today's announcement has already happened; only days
# that actually carry an announcement appear here.
_POST_ANNOUNCEMENT_OFFSETS: dict[int, tuple[int, int]] = {
    0: (-3, 0),   # Monday    → Fri 14:00 – Mon 14:00  (3-day)
    1: (-1, 0),   # Tuesday   → Mon 14:00 – Tue 14:00
    2: (-1, 0),   # Wednesday → Tue 14:00 – Wed 14:00
    3: (-1, 0),   # Thursday  → Wed 14:00 – Thu 14:00
    6: (-3, -2),  # Sunday    → Thu 14:00 – Fri 14:00
}


def get_submission_window() -> tuple[datetime, datetime]:
    """Return (window_start, window_end) for the most recent arXiv announcement.

    Both datetimes are timezone-aware (America/New_York). Raises ValueError on
    Saturday, or any time when no announcement data is available.
    """
    now = datetime.now(tz=_ET)
    today = now.date()
    weekday = today.weekday()  # Monday=0 … Sunday=6

    if now.hour >= _ANNOUNCEMENT_HOUR and weekday in _POST_ANNOUNCEMENT_OFFSETS:
        start_off, end_off = _POST_ANNOUNCEMENT_OFFSETS[weekday]
    elif weekday in _PRE_ANNOUNCEMENT_OFFSETS:
        start_off, end_off = _PRE_ANNOUNCEMENT_OFFSETS[weekday]
    else:
        raise ValueError(
            f"Cannot determine arXiv announcement window for weekday {weekday} "
            "at the current time (Saturday has no announcement)."
        )

    def _cutoff(offset: int) -> datetime:
        d = today + timedelta(days=offset)
        return datetime(d.year, d.month, d.day, _CUTOFF_HOUR, 0, 0, tzinfo=_ET)

    return _cutoff(start_off), _cutoff(end_off)


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


def fetch_papers(
    category: str, window_start: datetime, window_end: datetime
) -> list[dict]:
    """Fetch papers from an arXiv category within the given submission window.

    Retrieves up to MAX_RESULTS recent papers (sorted newest-first) and keeps
    only those whose ``<published>`` timestamp falls in [window_start, window_end).
    Returns a list of dicts with keys: ``title``, ``authors``, ``abstract``, ``id``.
    Raises ``httpx.HTTPStatusError`` on non-200 responses.
    Silently skips malformed or undatable XML entries.
    """
    params = {
        "search_query": f"cat:{category}",
        "max_results": MAX_RESULTS,
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
            published_el = entry.find(f"{{{ARXIV_NS}}}published")
            if any(
                el is None
                for el in (title_el, abstract_el, id_el, published_el)
            ):
                continue

            # Parse the UTC timestamp ("2026-06-14T18:00:00Z")
            published = datetime.fromisoformat(
                (published_el.text or "").strip().replace("Z", "+00:00")
            )

            if not (window_start <= published < window_end):
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
        except (AttributeError, TypeError, ValueError):
            continue

    return papers


def select_best(papers: list[dict], category: str, n: int | None = None) -> list[dict]:
    """Use the LLM to select and rank the n most relevant papers.

    Sends a numbered list of titles + truncated abstracts and expects a JSON
    array of integer indices back, ordered from most to least relevant.
    Raises on parse failure or wrong type. Silently drops out-of-range indices.
    """
    if n is None:
        n = SELECT_N
    # Truncate abstracts here to keep the prompt within the model's context window.
    # Full abstracts are used later in format_digest where summaries are written.
    numbered = "\n\n".join(
        f"[{i}] {p['title']}\nAuthors: {', '.join(p['authors'])}\n{p['abstract'][:400]}"
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
    model = LLM_MODEL

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=32768,
    )

    raw = (completion.choices[0].message.content or "").strip()

    # Extract the first JSON array from the response, tolerating markdown code
    # fences or leading/trailing explanation text.
    match = re.search(r"\[[\s\S]*?\]", raw)
    if not match:
        raise ValueError(
            f"No JSON array found in LLM response for category '{category}': {raw!r}"
        )

    try:
        indices = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned unparseable JSON for category '{category}': {raw!r}"
        ) from exc

    if not isinstance(indices, list):
        raise ValueError(
            f"LLM response is not a list for category '{category}': {raw!r}"
        )

    valid = [i for i in indices if isinstance(i, int) and 0 <= i < len(papers)]
    return [papers[i] for i in valid], completion.usage


def format_digest(results: dict[str, list[dict]]) -> str:
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
        f"Researcher Profile:\n{RESEARCHER_PROFILE}\n ################### \n"
        "You are writing a personalised daily arXiv digest email for researchers. Find their names and interests from the description above. Thereafter, folow the "
        f"formatting instructions:\n{OUTPUT_INSTRUCTIONS}"
    )
    user_prompt = (
        "Here are today's selected papers, already ranked from most to least "
        "relevant within each category:\n\n"
        f"{papers_text}\n\n"
        "Write the digest email body now."
    )

    client = _llm_client()
    model = LLM_MODEL

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=8000,
    )

    return (completion.choices[0].message.content or "").strip(), completion.usage


def send_email(subject: str, body: str) -> None:
    """Send the digest email via Gmail SMTP over SSL (port 465).

    Both sender and recipient are the configured Gmail address (self-send).
    SMTP exceptions propagate so GitHub Actions marks the run as failed.
    """
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    html = md.markdown(body, extensions=["extra"])
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{EMAIL_DISPLAY_NAME} <{address}>"
    msg["To"] = EMAIL_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        smtp.sendmail(address, [EMAIL_TO], msg.as_string())


_MATTERMOST_MAX_LEN = 4000


def _chunk_message(text: str) -> list[str]:
    """Split text at paragraph boundaries so no chunk exceeds the Mattermost limit."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for para in paragraphs:
        sep = 2 if current_parts else 0
        if current_len + sep + len(para) <= _MATTERMOST_MAX_LEN:
            current_parts.append(para)
            current_len += sep + len(para)
        else:
            if current_parts:
                chunks.append("\n\n".join(current_parts))
            current_parts = [para]
            current_len = len(para)

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks


def send_mattermost(body: str) -> None:
    """Post the digest to a Mattermost incoming webhook.

    Splits at paragraph boundaries if the body exceeds Mattermost's ~16k
    character post limit, then posts each chunk sequentially.
    HTTP errors propagate so GitHub Actions marks the run as failed.
    """
    webhook_url = os.environ["MATTERMOST_WEBHOOK_URL"]
    chunks = _chunk_message(body)
    print(f"--- DIGEST OUTPUT ---\n{body}\n--- END DIGEST OUTPUT ---")
    print(f"body length: {len(body)} chars, split into {len(chunks)} chunk(s)")
    for i, chunk in enumerate(chunks):
        print(f"chunk {i + 1}/{len(chunks)}: {len(chunk)} chars")
        response = httpx.post(webhook_url, json={"text": chunk}, timeout=30)
        if response.is_error:
            print(f"Mattermost webhook error {response.status_code}: {response.text}")
            response.raise_for_status()


def main() -> None:
    """Entry point: fetch, select, format, and deliver the daily arXiv digest."""
    parser = argparse.ArgumentParser(description="arXiv daily digest bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to config YAML file (default: config.yaml next to this script)",
    )
    args = parser.parse_args()

    global CATEGORIES, SELECT_N, MAX_RESULTS, RESEARCHER_PROFILE, OUTPUT_INSTRUCTIONS
    global LLM_BASE_URL, LLM_MODEL, DELIVERY, EMAIL_SUBJECT, EMAIL_TO, EMAIL_DISPLAY_NAME

    cfg = _load_config(args.config)
    CATEGORIES = cfg["categories"]
    SELECT_N = cfg["select_n"]
    MAX_RESULTS = cfg["max_results"]
    RESEARCHER_PROFILE = cfg["researcher_profile"].strip()
    OUTPUT_INSTRUCTIONS = cfg["output_instructions"].strip()
    LLM_BASE_URL = cfg["llm"]["base_url"]
    LLM_MODEL = cfg["llm"]["model"]
    DELIVERY = cfg["delivery"]
    EMAIL_SUBJECT = cfg["email"]["subject"]
    EMAIL_TO = cfg["email"]["to"]
    EMAIL_DISPLAY_NAME = cfg["email"]["display_name"]

    print(f"Using config: {args.config}")
    window_start, window_end = get_submission_window()
    print(f"Submission window: {window_start.isoformat()} → {window_end.isoformat()} ET")

    counts: dict[str, int] = {}
    results: dict[str, list[dict]] = {}
    prompt_tokens = completion_tokens = 0
    for category in CATEGORIES:
        print(f"Fetching papers for category: {category}")
        papers = fetch_papers(category, window_start, window_end)
        counts[category] = len(papers)
        print(f"  {len(papers)} papers in window")
        selected, usage = select_best(papers, category)
        results[category] = selected
        prompt_tokens += usage.prompt_tokens
        completion_tokens += usage.completion_tokens

    body, usage = format_digest(results)
    prompt_tokens += usage.prompt_tokens
    completion_tokens += usage.completion_tokens

    lines = ["P.S. In the most recent submission window:"] + [
        f"{cat}: {n} papers" for cat, n in counts.items()
    ]
    stats_footer = "\n".join(lines)

    if DELIVERY == "email":
        send_email(EMAIL_SUBJECT, body + "\n\n" + stats_footer)
        print("Digest sent via email.")
    elif DELIVERY == "mattermost":
        send_mattermost(body)
        send_mattermost(stats_footer)
        print("Digest posted to Mattermost.")
    else:
        raise ValueError(f"Unknown delivery method in config: {DELIVERY!r}")

    prices = _load_prices(Path(__file__).parent / "prices.csv")
    total_tokens = prompt_tokens + completion_tokens
    cost: float | None = None
    if LLM_MODEL in prices:
        price_in, price_out = prices[LLM_MODEL]
        cost = (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000

    token_line = (
        f"Model: {LLM_MODEL} | "
        f"Tokens — prompt: {prompt_tokens}, completion: {completion_tokens}, "
        f"total: {total_tokens}"
    )
    token_line += f" | Estimated cost: ${cost:.4f}" if cost is not None else " | Cost: model not in prices.csv"
    print(token_line)

    log_path = (Path(__file__).parent / "logs" / "consumption.csv").resolve()
    log_path.parent.mkdir(exist_ok=True)
    write_header = not log_path.exists()
    now = datetime.now(tz=_ET)
    with log_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "model", "time", "tokens_in", "tokens_out", "estimated_price_dollars"])
        if write_header:
            writer.writeheader()
        writer.writerow({
            "date": now.strftime("%Y-%m-%d"),
            "model": LLM_MODEL,
            "time": now.strftime("%H:%M:%S"),
            "tokens_in": prompt_tokens,
            "tokens_out": completion_tokens,
            "estimated_price_dollars": f"{cost:.4f}" if cost is not None else "",
        })


if __name__ == "__main__":
    main()
