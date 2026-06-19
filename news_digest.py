"""
News Daily Digest Bot

Fetches entries from configured RSS feeds within a rolling time window,
scores and deduplicates them via an LLM, then crafts a personalised news
digest. Delivery is via Gmail SMTP or a Mattermost webhook, controlled by
the `delivery` key in news_config.yaml. All user-facing settings are read
from news_config.yaml (or a custom path via --config).

Author: Aritra Bal (ETP)
Date: 2026-06-19
"""

import argparse
import csv
import html as _html
import json
import os
import re
import smtplib
import socket
import time
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
import markdown as md
import weasyprint
import yaml
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Give each feed fetch a hard deadline so a slow server cannot stall the run.
socket.setdefaulttimeout(15)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_config(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def _load_prices(path: Path) -> dict[str, tuple[float, float]]:
    """Load model prices from a CSV with columns: model, usd_per_mtok_in, usd_per_mtok_out."""
    if not path.exists():
        return {}
    with path.open() as f:
        return {
            row["model"]: (float(row["usd_per_mtok_in"]), float(row["usd_per_mtok_out"]))
            for row in csv.DictReader(f)
        }


# Config globals — populated by main() after the --config argument is parsed.
RSS_LIST_PATH: Path = Path("rss_links.txt")
FETCH_PER_SOURCE: int = 30
TIME_WINDOW_HOURS: int = 24
MAX_ITEMS_TO_LLM: int = 80
SELECT_N: int = 10
READER_PROFILE: str = ""
OUTPUT_INSTRUCTIONS: str = ""
LLM_BASE_URL: str = ""
LLM_MODEL: str = ""
CREATE_PDF: bool = True
FILENAME_SUFFIX: str | None = None
DELIVER_EMAIL: bool = False
EMAIL_AS_ATTACHMENT: bool = False
DELIVER_MATTERMOST: bool = False
EMAIL_SUBJECT: str = ""
EMAIL_TO: str = ""
EMAIL_DISPLAY_NAME: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_client() -> OpenAI:
    return OpenAI(api_key=os.environ["KIT_LLM_KEY"], base_url=LLM_BASE_URL)


def _load_rss_links(path: Path) -> list[str]:
    links = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            links.append(line)
    return links


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities, collapsing whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(_html.unescape(text).split())


def _parse_published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _source_name(feed, url: str) -> str:
    return feed.feed.get("title") or urlparse(url).netloc or url


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def fetch_entries(urls: list[str]) -> list[dict]:
    """Fetch RSS entries from all feeds, apply the time window filter, and cap.

    Returns entries sorted newest-first, capped at MAX_ITEMS_TO_LLM.
    Feeds that fail to load are skipped with a warning.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)
    all_entries: list[dict] = []

    for url in urls:
        try:
            feed = feedparser.parse(url, agent="news-digest/1.0")
        except Exception as exc:
            print(f"  Warning: could not fetch {url}: {exc}")
            continue

        source = _source_name(feed, url)
        count = 0
        for entry in feed.entries[:FETCH_PER_SOURCE]:
            pub = _parse_published(entry)
            if pub is None or pub < cutoff:
                continue

            # Prefer summary; fall back to content blocks or description.
            raw = (
                entry.get("summary")
                or entry.get("description")
                or next((c.get("value", "") for c in (entry.get("content") or [])), "")
            )
            snippet = _strip_html(raw)[:500]

            all_entries.append({
                "title": (entry.get("title") or "").strip(),
                "url": entry.get("link") or url,
                "source": source,
                "published": pub,
                "snippet": snippet,
            })
            count += 1

        print(f"  {source}: {count} entr{'y' if count == 1 else 'ies'} in window")
        time.sleep(0.3)  # be polite between feed fetches

    all_entries.sort(key=lambda e: e["published"], reverse=True)

    if len(all_entries) > MAX_ITEMS_TO_LLM:
        print(f"Capping to {MAX_ITEMS_TO_LLM} entries (from {len(all_entries)} after time filter)")
        all_entries = all_entries[:MAX_ITEMS_TO_LLM]

    return all_entries


def triage_entries(entries: list[dict]) -> tuple[list[dict], object]:
    """LLM call 1: deduplicate, score, and select entries scoring >= 5.

    Sends all entries as a numbered list; expects a JSON array of
    {index, score} objects back, sorted by score descending. Silently drops
    items with out-of-range indices or malformed shapes.
    """
    numbered = "\n\n".join(
        f"[{i}] Source: {e['source']}\n"
        f"Title: {e['title']}\n"
        f"URL: {e['url']}\n"
        f"Snippet: {e['snippet'][:300]}"
        for i, e in enumerate(entries)
    )

    system_prompt = (
        f"{READER_PROFILE}\n\n"
        "Follow the STEP 1 (deduplication) and STEP 2 (scoring) instructions above. "
        "Return ONLY a JSON array of objects for entries scoring 5 or above, "
        "sorted by score descending. Each object must have exactly two keys: "
        '"index" (integer, 0-based) and "score" (float 0-10). '
        "No explanation, no markdown fences -- raw JSON array only. "
        "Entries removed as duplicates must not appear in the output at all."
    )
    user_prompt = (
        f"Here are {len(entries)} news entries from the past {TIME_WINDOW_HOURS} hours:\n\n"
        f"{numbered}\n\n"
        "Deduplicate, score, and return the JSON array."
    )

    client = _llm_client()
    completion = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=4096,
    )

    raw = (completion.choices[0].message.content or "").strip()

    # Tolerate markdown fences or leading explanation text.
    match = re.search(r"\[[\s\S]*?\]", raw)
    if not match:
        raise ValueError(f"No JSON array found in triage response: {raw!r}")

    try:
        scored = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned unparseable triage JSON: {raw!r}") from exc

    if not isinstance(scored, list):
        raise ValueError(f"Triage response is not a list: {raw!r}")

    result: list[dict] = []
    for item in scored:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        score = item.get("score")
        if not isinstance(idx, int) or not isinstance(score, (int, float)):
            continue
        if not (0 <= idx < len(entries)):
            continue
        entry = dict(entries[idx])
        entry["score"] = float(score)
        result.append(entry)

    return result, completion.usage


def format_digest(entries: list[dict]) -> tuple[str, object]:
    """LLM call 2: write the full personalised digest from scored entries.

    Entries are passed with their scores so the model can assign them to
    the correct output section (Breakthroughs / Notable / Worth Noting).
    """
    if not entries:
        return "No items scored above the threshold in this window.", None

    items_text = "\n\n".join(
        f"[Score: {e['score']:.1f}] {e['source']}: {e['title']}\n"
        f"URL: {e['url']}\n"
        f"Snippet: {e['snippet']}"
        for e in entries
    )

    system_prompt = (
        f"Reader Profile:\n{READER_PROFILE}\n"
        "###################\n"
        f"Output Instructions:\n{OUTPUT_INSTRUCTIONS}"
    )
    user_prompt = (
        "Here are today's scored news items, sorted highest score first. "
        "Use each item's score to place it in the correct output section and "
        "apply the per-section caps from the output instructions.\n\n"
        f"{items_text}\n\n"
        "Write the digest now."
    )

    client = _llm_client()
    completion = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=8000,
    )

    return (completion.choices[0].message.content or "").strip(), completion.usage


def create_pdf(body: str) -> Path:
    """Render the Markdown digest body to a PDF and save it under ./digests/."""
    html_body = md.markdown(body, extensions=["extra"])
    out_dir = (Path(__file__).parent / "digests").resolve()
    out_dir.mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%d-%m-%Y")
    filename = f"{date_str}-{FILENAME_SUFFIX}.pdf" if FILENAME_SUFFIX else f"{date_str}.pdf"
    pdf_path = out_dir / filename
    weasyprint.HTML(string=html_body).write_pdf(pdf_path)
    print(f"PDF saved to {pdf_path}")
    return pdf_path


def send_email(subject: str, body: str, attachment: Path | None = None) -> None:
    """Send the digest email via Gmail SMTP over SSL (port 465)."""
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    if attachment:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = f"{EMAIL_DISPLAY_NAME} <{address}>"
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(
            "<p>Your daily news digest is attached.</p>",
            "html", "utf-8",
        ))
        with attachment.open("rb") as f:
            pdf_part = MIMEApplication(f.read(), _subtype="pdf")
        pdf_part.add_header("Content-Disposition", "attachment", filename=attachment.name)
        msg.attach(pdf_part)
    else:
        html_body = md.markdown(body, extensions=["extra"])
        msg = MIMEText(html_body, "html", "utf-8")
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
    """Post the digest to Mattermost via the incoming webhook."""
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
    """Entry point: fetch, triage, format, and deliver the daily news digest."""
    parser = argparse.ArgumentParser(description="News daily digest bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "news_config.yaml",
        help="Path to config YAML file (default: news_config.yaml next to this script)",
    )
    args = parser.parse_args()

    global RSS_LIST_PATH, FETCH_PER_SOURCE, TIME_WINDOW_HOURS, MAX_ITEMS_TO_LLM, SELECT_N
    global READER_PROFILE, OUTPUT_INSTRUCTIONS, LLM_BASE_URL, LLM_MODEL
    global CREATE_PDF, FILENAME_SUFFIX
    global DELIVER_EMAIL, EMAIL_AS_ATTACHMENT, EMAIL_SUBJECT, EMAIL_TO, EMAIL_DISPLAY_NAME
    global DELIVER_MATTERMOST

    cfg = _load_config(args.config)

    rss_path = Path(cfg["rss_list"])
    if not rss_path.is_absolute():
        rss_path = args.config.parent / rss_path
    RSS_LIST_PATH = rss_path

    FETCH_PER_SOURCE = int(cfg.get("fetch_per_source", 30))
    TIME_WINDOW_HOURS = int(cfg.get("time_window_hours", 24))
    MAX_ITEMS_TO_LLM = int(cfg.get("max_items_to_llm", 80))
    SELECT_N = int(cfg.get("select_n", 10))
    READER_PROFILE = cfg["reader_profile"].strip()
    OUTPUT_INSTRUCTIONS = cfg["output_instructions"].strip()
    LLM_BASE_URL = cfg["llm"]["base_url"]
    LLM_MODEL = cfg["llm"]["model"]
    CREATE_PDF = bool(cfg.get("create_pdf", True))
    FILENAME_SUFFIX = cfg.get("filename_suffix") or None

    delivery_cfg = cfg.get("delivery", {})

    mm_cfg = delivery_cfg.get("mattermost", {})
    DELIVER_MATTERMOST = bool(mm_cfg.get("enabled", False))

    email_cfg = delivery_cfg.get("email", {})
    DELIVER_EMAIL = bool(email_cfg.get("enabled", False))
    EMAIL_AS_ATTACHMENT = bool(email_cfg.get("as_attachment", False))
    if DELIVER_EMAIL:
        EMAIL_SUBJECT = email_cfg["subject"]
        EMAIL_TO = email_cfg["to"]
        EMAIL_DISPLAY_NAME = email_cfg["display_name"]

    attachment_requested = DELIVER_EMAIL and EMAIL_AS_ATTACHMENT
    if attachment_requested and not CREATE_PDF:
        print("Warning: as_attachment is enabled but create_pdf is false -- creating PDF anyway.")

    need_pdf = CREATE_PDF or attachment_requested
    if not DELIVER_EMAIL and not DELIVER_MATTERMOST and not need_pdf:
        raise ValueError(
            "Nothing to do: no delivery method enabled and create_pdf is false. "
            "Set mattermost.enabled, email.enabled, or create_pdf: true in config."
        )

    print(f"Using config: {args.config}")

    urls = _load_rss_links(RSS_LIST_PATH)
    print(f"Loaded {len(urls)} RSS feeds | window: last {TIME_WINDOW_HOURS}h")

    entries = fetch_entries(urls)
    print(f"Total entries after time filter: {len(entries)}")

    if not entries:
        print("No entries found in the time window. Exiting.")
        return

    print(f"Triaging {len(entries)} entries with LLM...")
    scored_entries, triage_usage = triage_entries(entries)
    print(f"{len(scored_entries)} entries scored >= 5")

    print("Formatting digest...")
    body, format_usage = format_digest(scored_entries)

    stats_footer = (
        f"P.S. {len(urls)} RSS feeds | last {TIME_WINDOW_HOURS}h | "
        f"{len(entries)} entries fetched, {len(scored_entries)} scored >= 5"
    )

    pdf_path: Path | None = None
    if need_pdf:
        pdf_path = create_pdf(body + "\n\n" + stats_footer)

    if DELIVER_EMAIL:
        if EMAIL_AS_ATTACHMENT:
            send_email(EMAIL_SUBJECT, body, attachment=pdf_path)
        else:
            send_email(EMAIL_SUBJECT, body + "\n\n" + stats_footer)
        print("Digest sent via email.")

    if DELIVER_MATTERMOST:
        send_mattermost(body)
        send_mattermost(stats_footer)
        print("Digest posted to Mattermost.")

    prompt_tokens = completion_tokens = 0
    for usage in (triage_usage, format_usage):
        if usage:
            prompt_tokens += usage.prompt_tokens
            completion_tokens += usage.completion_tokens

    prices = _load_prices(Path(__file__).parent / "prices.csv")
    total_tokens = prompt_tokens + completion_tokens
    cost: float | None = None
    if LLM_MODEL in prices:
        price_in, price_out = prices[LLM_MODEL]
        cost = (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000

    token_line = (
        f"Model: {LLM_MODEL} | "
        f"Tokens -- prompt: {prompt_tokens}, completion: {completion_tokens}, "
        f"total: {total_tokens}"
    )
    token_line += f" | Estimated cost: ${cost:.4f}" if cost is not None else " | Cost: model not in prices.csv"
    print(token_line)

    log_path = (Path(__file__).parent / "logs" / "consumption.csv").resolve()
    log_path.parent.mkdir(exist_ok=True)
    write_header = not log_path.exists()
    now = datetime.now(timezone.utc)
    with log_path.open("a", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "model", "time", "tokens_in", "tokens_out", "estimated_price_dollars"],
        )
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
