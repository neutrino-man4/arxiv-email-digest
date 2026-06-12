# arXiv Email Digest

A GitHub Actions bot that fetches recent arXiv papers, uses an LLM to select and rank the most relevant ones for your research, and emails you a personalised daily digest in HTML.

## How it works

1. Fetches the most recent papers from each configured arXiv category via the arXiv API
2. Calls an LLM to select and rank the most relevant papers per category, guided by your researcher profile
3. Calls the LLM a second time to write a personalised email — with top-pick summaries, clickable links, and formatting of your choice
4. Sends the digest as an HTML email from a Gmail account to any address you specify

The digest runs on a daily cron schedule via GitHub Actions. No server required.

---

## Configuration

Everything you need to customise lives in `config.yaml`. You never need to touch the Python script.

```yaml
categories:
  - hep-ph
  - hep-ex
  - cs.LG

fetch_n: 50       # papers fetched from arXiv per category
select_n: 10      # papers selected by the LLM per category

user:
  name: Alice

researcher_profile: |
  Describe your research interests here. The LLM uses this to decide which
  papers are most relevant and how to rank them. Be specific — mention topics,
  methods, and application areas. You can also name preferred authors:
  "Prioritise papers by John Smith and Jane Doe." To suppress your own papers:
  "Ignore papers on which I (Alice Example) am a co-author."

output_instructions: |
  Controls how the email is written. Specify tone (formal/informal), structure,
  summary length, what to emphasise, etc. The output is Markdown rendered to HTML,
  so you can ask for **bold**, *italics*, and [clickable links](url).
  Example: "Write informally. Open with a Top Picks section covering the 2 most
  relevant papers with 3-4 sentence summaries. List the rest by category with
  title and link only. Close with a casual sign-off."

llm:
  base_url: https://your-llm-endpoint/api   # any OpenAI-compatible endpoint
  model: your-model-name

email:
  subject: "[arXiv] Daily Digest"
  to: you@yourinstitution.edu
```

### arXiv category IDs

Find valid category strings at [arxiv.org/category_taxonomy](https://arxiv.org/category_taxonomy). Examples: `hep-ph`, `hep-ex`, `cs.LG`, `quant-ph`, `astro-ph.HE`.

### LLM endpoint

The bot works with any OpenAI-compatible API — OpenAI, Anthropic (via proxy), local Ollama, or institutional endpoints. Set `llm.base_url` and `llm.model` in `config.yaml`. The API key is kept as a GitHub secret (see Setup below).

---

## Setup

### 1. Fork or clone this repository

Push it to your own GitHub account.

### 2. Add GitHub Actions secrets

Go to your repo on GitHub → *Settings → Secrets and variables → Actions → New repository secret* and add:

| Secret | Description |
|---|---|
| `LLM_API_KEY` | API key for your LLM endpoint |
| `GMAIL_ADDRESS` | Gmail address the digest is sent *from* |
| `GMAIL_APP_PASSWORD` | Gmail App Password (see step 3) |

Then update `.github/workflows/daily.yml` to pass `LLM_API_KEY` instead of `KIT_LLM_KEY` if you renamed it, or keep the name as-is and just set the secret value.

### 3. Generate a Gmail App Password

The bot sends email via Gmail SMTP. It needs an App Password, not your account password.

1. Enable 2-Step Verification on your Google account
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create a password for "Mail / Other" and copy the 16-character result — that is your `GMAIL_APP_PASSWORD`

The sender and recipient can be different addresses. Set `email.to` in `config.yaml` to wherever you want the digest delivered.

### 4. Set the schedule

The workflow runs at **07:00 UTC** by default. Edit the cron line in `.github/workflows/daily.yml`:

```yaml
- cron: "0 7 * * *"   # runs at 07:00 UTC every day
```

GitHub Actions cron is always UTC — convert your preferred local time accordingly. For example, 09:30 UTC = 10:30 CET (winter) / 11:30 CEST (summer).

### 5. Test it manually

Go to your repo → *Actions → arXiv Daily Digest → Run workflow*. This triggers an immediate run using the same job as the cron. Watch the logs to confirm everything works before relying on the schedule.

---

## Local testing

```bash
pip install -r requirements.txt

export KIT_LLM_KEY=...        # or whatever you named your API key secret
export GMAIL_ADDRESS=...
export GMAIL_APP_PASSWORD=...

python arxiv_digest.py
```

To verify only the LLM connection without sending email, use the included smoke test:

```bash
python test_kit_api.py
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | LLM API client (OpenAI-compatible) |
| `httpx` | arXiv API requests |
| `pyyaml` | Reading `config.yaml` |
| `markdown` | Converting LLM output to HTML for the email |

Everything else (`smtplib`, `xml`, `json`, etc.) is Python standard library.
