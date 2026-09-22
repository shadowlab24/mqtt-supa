#!/usr/bin/env python3
"""
retry_failed.py — replay messages from failed_messages.csv into Supabase.

Run manually after connectivity is restored:
    python retry_failed.py

On success each row is removed. On first Supabase error the script stops
and leaves remaining rows untouched so you can fix the problem and retry.

Environment variables (same as the main script):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    SUPABASE_TABLE      (default: mqtt_messages)
    FAILED_CSV          (default: failed_messages.csv)
    DRAIN_BATCH         rows per request (default: 50)
"""

import csv
import json
import logging
import os
import sys
import tempfile

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _require(key):
    val = os.getenv(key)
    if not val:
        print(f"ERROR: {key} is not set", file=sys.stderr)
        sys.exit(1)
    return val


SUPABASE_URL   = _require("SUPABASE_URL")
SUPABASE_KEY   = _require("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "mqtt_messages")
FAILED_CSV     = os.getenv("FAILED_CSV", "failed_messages.csv")
BATCH          = int(os.getenv("DRAIN_BATCH", "50"))

endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SUPABASE_TABLE}"
session  = requests.Session()
session.headers.update({
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=minimal",
})


def main():
    if not os.path.exists(FAILED_CSV):
        logger.info("No failed messages file found (%s) — nothing to do.", FAILED_CSV)
        return

    with open(FAILED_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        logger.info("No rows in %s — nothing to do.", FAILED_CSV)
        return

    logger.info("Rows to retry: %d (batch size: %d)", len(rows), BATCH)

    sent      = 0
    remaining = rows[:]

    while remaining:
        batch = remaining[:BATCH]

        messages = [
            {
                "received_at": row["received_at"],
                "topic":       row["topic"],
                "payload":     json.loads(row["payload"]),
            }
            for row in batch
        ]

        try:
            resp = session.post(endpoint, json=messages, timeout=10)
            if not resp.ok:
                logger.error("Supabase error %s: %s", resp.status_code, resp.text[:300])
                logger.error("Stopping — fix the error and run retry_failed.py again.")
                break
        except requests.RequestException as exc:
            logger.error("Connection error: %s", exc)
            logger.error("Stopping — check connectivity and run retry_failed.py again.")
            break

        remaining = remaining[BATCH:]
        sent += len(batch)
        logger.info("Sent %d / %d", sent, len(rows))

    # Rewrite the CSV with only the rows that were not sent.
    if not remaining:
        os.remove(FAILED_CSV)
        logger.info("All rows sent — %s removed.", FAILED_CSV)
    else:
        tmp = FAILED_CSV + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["failed_at", "received_at", "topic", "payload"])
            writer.writeheader()
            writer.writerows(remaining)
        os.replace(tmp, FAILED_CSV)
        logger.info(
            "Done. %d row(s) sent, %d row(s) remaining in %s.",
            sent, len(remaining), FAILED_CSV,
        )


if __name__ == "__main__":
    main()