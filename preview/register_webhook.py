"""One-shot registration of the Wrike webhook.

Usage:
    python preview/register_webhook.py \
        --hook-url https://example.trycloudflare.com/webhook \
        --secret devsecret

Prints the webhook id on success. Save it; you'll need it to delete the webhook later.
"""

import argparse
import json

import requests

from wrike import WrikeApi


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook-url", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    wrike = WrikeApi(args.config)
    url = f"{wrike.WRIKE_BASE_URL}/webhooks"
    headers = wrike.WRIKE_DEFAULT_HEADERS
    payload = {
        "hookUrl": args.hook_url,
        "secret": args.secret,
        "events": ["AttachmentAdded"],
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()["data"]
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
