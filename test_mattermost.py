"""
Quick sanity-check: post a hello-world message to the Mattermost webhook.

Author: Aritra Bal (ETP)
Date: 2026-06-15
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    webhook_url = os.environ["MATTERMOST_WEBHOOK_URL"]

    payload = {"text": "Hello from arxiv-digest test! Webhook is working."}
    response = httpx.post(webhook_url, json=payload, timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Body:   {response.text}")
    response.raise_for_status()
    print("Success.")


if __name__ == "__main__":
    main()
