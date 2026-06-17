# arXiv Daily Digest

A bot that fetches the day's new arXiv papers, uses an LLM to select and rank the most relevant ones for your research interests, and delivers a personalised digest — as an HTML email, a Mattermost post, a saved PDF, or any combination of these.

It can run as a **GitHub Actions** scheduled job (no server required) or as a **local cron job** on any Linux/Mac server.

## How it works

1. Determines the correct arXiv announcement window for the current day (based on arXiv's submission cutoff schedule, all times US Eastern)
2. Fetches all papers submitted in that window from each configured category
3. Calls an LLM to select and rank the most relevant papers, guided by your researcher profile
4. Calls the LLM a second time to write the digest — with top-pick summaries, clickable links, and formatting of your choice
5. Delivers via Gmail SMTP, a Mattermost incoming webhook, or saves a PDF locally — or any combination
6. Logs token consumption to `logs/consumption.csv` for cost tracking

---

## Configuration

Copy `base_config.yaml` to `config.yaml` and fill in your details. `config.yaml` is gitignored so your personal settings stay local.

```bash
cp base_config.yaml config.yaml
```

Key fields:

```yaml
categories:
  - hep-ph       # arXiv category IDs — see arxiv.org/category_taxonomy
  - hep-ex

select_n: 10     # papers selected by the LLM per category
max_results: 150 # safety cap on papers fetched per category (set above daily volume)

researcher_profile: |
  Describe who the digest is for and what topics they care about.
  For multiple researchers, list each with their topics separately.
  Include an AUTHOR BOOST list and a HARD EXCLUSION rule for self-citations.

output_instructions: |
  Controls tone, structure, and formatting of the digest body.

llm:
  base_url: https://your-llm-endpoint/   # any OpenAI-compatible API
  model: your-model-name

# Save a PDF to ./digests/DD-MM-YYYY.pdf after each run
create_pdf: true

delivery:
  mattermost:
    enabled: true           # post inline via webhook
    as_attachment: false    # if true, upload PDF via Mattermost API instead
  email:
    enabled: false          # send via Gmail SMTP
    as_attachment: false    # if true, send PDF as email attachment instead
    subject: "[arXiv] Daily Digest"
    to: you@example.com
    display_name: "arXiv Bot"
```

Multiple delivery methods can be active simultaneously — set `enabled: true` on as many as you like. If all delivery methods are disabled but `create_pdf: true`, the PDF is still saved locally.

**`as_attachment` mode** sends a short "here is your digest" message with the PDF attached instead of posting the full inline text. For Mattermost, file upload requires the REST API rather than a webhook; set three additional environment variables: `MATTERMOST_URL`, `MATTERMOST_TOKEN` (personal access or bot token), and `MATTERMOST_CHANNEL_ID`.

If you need different configurations (e.g. different researcher groups), keep multiple YAML files and pass the one you want at runtime:

```bash
python arxiv_digest.py --config my_other_config.yaml
```

---

## Option A — GitHub Actions (no server required)

### 1. Fork or clone this repository

### 2. Add repository secrets

Go to *Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Description |
|---|---|
| `KIT_LLM_KEY` | API key for your LLM endpoint |
| `MATTERMOST_WEBHOOK_URL` | Mattermost incoming webhook URL (inline Mattermost delivery) |
| `MATTERMOST_URL` | Mattermost server base URL — e.g. `https://mattermost.example.com` (attachment mode only) |
| `MATTERMOST_TOKEN` | Mattermost personal access or bot token (attachment mode only) |
| `MATTERMOST_CHANNEL_ID` | Channel to post to (attachment mode only) |
| `GMAIL_ADDRESS` | Gmail address to send from (email delivery) |
| `GMAIL_APP_PASSWORD` | Gmail App Password — see [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |

### 3. Commit your config

`config.yaml` is gitignored. Either add it to the repository manually or adjust the workflow to pass it in another way (e.g. via a secret or environment variable).

### 4. Adjust the schedule

Edit the cron line in `.github/workflows/daily.yml`. The workflow uses the `Europe/Berlin` timezone:

```yaml
- cron: "57 12 * * 1-5"
  timezone: "Europe/Berlin"
```

### 5. Test manually

Go to *Actions → arXiv Daily Digest → Run workflow* to trigger an immediate run.

---

## Option B — Local conda environment + cron

### 1. Create the conda environment

```bash
conda create -n arxiv-digest python=3.11
conda activate arxiv-digest
pip install -r requirements.txt
```

### 2. Set up credentials

Create a `.env` file in the project root (it is gitignored):

```bash
KIT_LLM_KEY=your_api_key_here
MATTERMOST_WEBHOOK_URL=https://your.mattermost.server/hooks/xxx   # inline webhook delivery
MATTERMOST_URL=https://your.mattermost.server                     # attachment mode only
MATTERMOST_TOKEN=your_bot_or_personal_token                       # attachment mode only
MATTERMOST_CHANNEL_ID=your_channel_id                             # attachment mode only
GMAIL_ADDRESS=you@gmail.com           # email delivery only
GMAIL_APP_PASSWORD=your_app_password  # email delivery only
```

### 3. Test a manual run

```bash
conda activate arxiv-digest
python arxiv_digest.py
```

### 4. Add a crontab entry

Open your crontab with `crontab -e` and add a line. The example below runs the digest at 09:59 on weekdays and logs output to `logs/cron.log`:

```
59 9 * * 1-5 cd /path/to/arxiv-email-digest && /path/to/miniconda/bin/conda run -n arxiv-digest python arxiv_digest.py >> /path/to/arxiv-email-digest/logs/cron.log 2>&1
```

Replace `/path/to/` with your actual paths. To find your conda binary: `which conda`.

To also run the weekly usage report every Monday at 09:00:

```
0 9 * * 1 cd /path/to/arxiv-email-digest && /path/to/miniconda/bin/conda run -n arxiv-digest python usage_report.py >> /path/to/arxiv-email-digest/logs/cron.log 2>&1
```

---

## Token consumption tracking

After each run the script prints a summary and appends a row to `logs/consumption.csv`:

```
Model: azure.gpt-5-mini | Tokens — prompt: 18432, completion: 2104, total: 20536 | Estimated cost: $0.0098
```

To add pricing for your model, create a `prices.csv` file (gitignored) in the project root:

```csv
model,usd_per_mtok_in,usd_per_mtok_out
your-model-name,0.28,2.20
```

Run `python usage_report.py` at any time to post a weekly summary to Mattermost.

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | LLM API client (OpenAI-compatible) |
| `httpx` | arXiv API and Mattermost webhook/API requests |
| `pyyaml` | Config file parsing |
| `markdown` | Converts digest Markdown to HTML for email and PDF rendering |
| `weasyprint` | Renders HTML to PDF for `create_pdf` and `as_attachment` modes |
| `python-dotenv` | Loads credentials from `.env` for local runs |
| `tzdata` | IANA timezone data for portable `zoneinfo` support |

Everything else (`smtplib`, `xml`, `csv`, `json`, `argparse`, etc.) is Python standard library.

`weasyprint` requires system libraries for font and layout rendering. Install them once:

```bash
# Ubuntu / Debian
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b

# macOS
brew install pango
```
