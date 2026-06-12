# arXiv Email Digest

A GitHub Actions bot that fetches recent arXiv papers, selects the most relevant ones via an LLM, and emails you a daily digest.

## How it works

1. Fetches the 50 most recent papers from each configured arXiv category
2. Asks an LLM to pick the 10 most relevant per category, given a researcher profile
3. Sends a plain-text email with titles and links

## Customisation

### Categories and fetch count

In `arxiv_digest.py`:

```python
CATEGORIES = ["hep-ph", "hep-ex", "cs.LG", "quant-ph"]  # arXiv category IDs
FETCH_N = 50  # papers fetched before LLM selection
```

### Researcher profile

Edit `RESEARCHER_PROFILE` in `arxiv_digest.py` to describe your research interests. This string is passed directly to the LLM as selection context.

### LLM endpoint

The bot uses an OpenAI-compatible API. To point it at a different provider, change `base_url` in `_kit_client()`:

```python
client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url="https://your-llm-endpoint/api",
)
```

Update the env variable names in `_kit_client()` and the workflow file to match.

## Setup

### 1. GitHub Actions secrets

Add the following secrets under *Settings → Secrets and variables → Actions*:

| Secret | Description |
|---|---|
| `KIT_LLM_KEY` | API key for your LLM endpoint |
| `KIT_LLM_MODEL` | Model name, e.g. `kit.qwen3.5-397b-A17b` |
| `GMAIL_ADDRESS` | Gmail address to send from (and to) |
| `GMAIL_APP_PASSWORD` | [Gmail App Password](https://myaccount.google.com/apppasswords) — not your account password |

### 2. Gmail App Password

Enable 2-Step Verification on your Google account, then generate an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

### 3. Schedule

The workflow runs daily at **07:00 UTC** by default. Change the cron expression in `.github/workflows/daily.yml` to suit your timezone. You can also trigger it manually via *Actions → workflow_dispatch*.

### 4. Forwarding (optional)

To auto-forward the digest to another address, create a Gmail filter matching `Subject: [arXiv]` and set it to forward then delete.

## Local testing

```bash
pip install -r requirements.txt

export KIT_LLM_KEY=...
export KIT_LLM_MODEL=...
export GMAIL_ADDRESS=...
export GMAIL_APP_PASSWORD=...

python arxiv_digest.py
```

## Dependencies

`openai`, `httpx` — everything else is Python standard library.
