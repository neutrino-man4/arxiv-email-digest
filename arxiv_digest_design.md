# arXiv Daily Digest Bot — Design Document

## Purpose

A scheduled script that fetches recent arXiv papers across specified categories, selects the 10 most relevant per category via an LLM, and delivers the digest as an email.

---

## Delivery Pipeline

```
GitHub Actions cron (07:00 UTC)
  → arxiv_digest.py
  → arXiv API (fetch 50 papers per category)
  → KIT LLM API (select best 10 per category)
  → Gmail SMTP (send to self)
  → Gmail filter (forward to KIT address, delete Gmail copy)
```

---

## Repository Structure

```
arxiv-digest/
├── .github/
│   └── workflows/
│       └── daily.yml
├── arxiv_digest.py
└── requirements.txt
```

---

## Configuration

### arXiv Categories

```python
CATEGORIES = ["hep-ph", "hep-ex", "cs.LG", "quant-ph"]
FETCH_N = 50  # papers fetched before LLM selection
```

### Researcher Profile (used in LLM prompt)

```
The reader is a particle physicist and ML researcher working on
Lorentz-equivariant neural networks, jet tagging, quantum machine learning
(continuous-variable QML, qumode architectures), anomaly detection at CMS,
simulation-based inference, and information geometry (Fisher information,
natural gradient). Prioritise papers directly relevant to these topics.
```

---

## Dependencies

```
openai
httpx
```

Standard library only beyond these: `smtplib`, `email`, `xml.etree.ElementTree`, `json`, `os`.

---

## Environment Variables

| Variable | Description |
|---|---|
| `KIT_LLM_KEY` | API key for KIT LLM endpoint |
| `KIT_LLM_MODEL` | kit.qwen3.5-397b-A17b |
| `GMAIL_ADDRESS` | Gmail address used for sending |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not account password) |

### LLM Endpoint

- **Base URL:** `https://ki-toolbox.scc.kit.edu/api`
- **Compatibility:** OpenAI-compatible; use the `openai` Python package with `base_url` set to the above
- **Client instantiation:**

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["KIT_LLM_KEY"],
    base_url="https://ki-toolbox.scc.kit.edu/api",
)
```

---

## Module Breakdown

### `fetch_papers(category: str, n: int) -> list[dict]`

- Calls `https://export.arxiv.org/api/query`
- Parameters: `search_query=cat:{category}`, `max_results=n`, `sortBy=submittedDate`, `sortOrder=descending`
- Parses Atom XML response
- Returns list of dicts with keys: `title`, `abstract`, `id`

### `select_best(papers: list[dict], category: str) -> list[dict]`

- Calls KIT LLM via OpenAI-compatible client
- Sends numbered list of `[i] title\nabstract[:400]`
- System/user prompt instructs model to return **only** a JSON array of 10 integer indices
- Parses response with `json.loads`; guards against out-of-range indices
- Returns the 10 selected paper dicts
- `temperature=0.0`, `max_tokens=256`

### `build_email_body(results: dict[str, list[dict]]) -> str`

- Formats all categories into a single plain-text string
- Per paper: index, title, arXiv URL

### `send_email(subject: str, body: str) -> None`

- Gmail SMTP over SSL, port 465
- From and To both set to `GMAIL_ADDRESS` (self-send)
- Subject must contain `[arXiv]` to match the Gmail forwarding filter

### `main()`

- Iterates over `CATEGORIES`
- Calls `fetch_papers` → `select_best` per category
- Aggregates results → `build_email_body` → `send_email`

---

## GitHub Actions Workflow

```yaml
on:
  schedule:
    - cron: "0 7 * * *"
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python arxiv_digest.py
        env:
          KIT_LLM_KEY:        ${{ secrets.KIT_LLM_KEY }}
          KIT_LLM_MODEL:      ${{ secrets.KIT_LLM_MODEL }}
          GMAIL_ADDRESS:      ${{ secrets.GMAIL_ADDRESS }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
```

---

## Error Handling Requirements

- `fetch_papers`: raise on non-200 HTTP response; skip malformed XML entries
- `select_best`: if `json.loads` fails or returns wrong type, raise with informative message; filter out-of-range indices silently
- `send_email`: let SMTP exceptions propagate (GitHub Actions will mark the run as failed, providing visibility)

---

## Code Style

- Python 3.11
- Full type hints throughout
- Docstrings on all functions
- Module-level docstring with purpose, author (`Aritra Bal (ETP)`), and date
- No global mutable state; all config via environment variables or module-level constants
