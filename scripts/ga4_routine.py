"""
Cloud-routine entrypoint for GA4 pulls.

Designed to run inside an Anthropic cloud routine (no local files): it reads the
service-account key from an ENVIRONMENT VARIABLE (GA4_SA_JSON_CONTENT, the full JSON
as a string) rather than a file, and takes the query as CLI args so the routine can
translate a user's natural-language prompt into a concrete report.

Falls back to a local key file (GA4_SA_JSON) if the inline env var is absent, so the
same script also runs locally.

Examples (the routine picks args from the user's prompt):
    python scripts/ga4_routine.py --days 30 --google-ads-only
    python scripts/ga4_routine.py --days 7
    python scripts/ga4_routine.py --days 90 --limit 50

Env:
    GA4_PROPERTY_ID        (required) numeric GA4 property id
    GA4_SA_JSON_CONTENT    full service-account JSON as a single string (cloud)
    GA4_SA_JSON            path to a service-account JSON file (local fallback)
"""
from __future__ import annotations

import os
import sys
import json
import argparse

try:  # optional: load .env locally; cloud routine uses real env vars
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest, OrderBy,
)

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def credentials():
    inline = os.getenv("GA4_SA_JSON_CONTENT", "").strip()
    if inline:
        try:
            info = json.loads(inline)
        except json.JSONDecodeError as e:
            die(f"GA4_SA_JSON_CONTENT is not valid JSON: {e}")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    path = os.getenv("GA4_SA_JSON", "").strip()
    if path and os.path.exists(path):
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    die("No service-account key found. Set GA4_SA_JSON_CONTENT (inline JSON) in the "
        "cloud environment, or GA4_SA_JSON (file path) locally.")


def main() -> None:
    ap = argparse.ArgumentParser(description="GA4 acquisition pull (cloud-routine friendly).")
    ap.add_argument("--days", type=int, default=30, help="look-back window in days (default 30)")
    ap.add_argument("--google-ads-only", action="store_true",
                    help="only rows where source=google and medium=cpc")
    ap.add_argument("--limit", type=int, default=100, help="max rows (default 100)")
    args = ap.parse_args()

    prop = os.getenv("GA4_PROPERTY_ID", "").strip()
    if not prop:
        die("GA4_PROPERTY_ID not set.")

    client = BetaAnalyticsDataClient(credentials=credentials())
    req = RunReportRequest(
        property=f"properties/{prop}",
        date_ranges=[DateRange(start_date=f"{args.days}daysAgo", end_date="today")],
        dimensions=[
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
            Dimension(name="sessionCampaignName"),
        ],
        metrics=[
            Metric(name="sessions"),
            Metric(name="conversions"),
            Metric(name="totalUsers"),
        ],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="conversions"), desc=True)],
        limit=args.limit,
    )
    resp = client.run_report(req)

    rows = []
    for r in resp.rows:
        src, med, camp = (d.value for d in r.dimension_values)
        sess, conv, users = (m.value for m in r.metric_values)
        if args.google_ads_only and not (src.lower() == "google" and med.lower() == "cpc"):
            continue
        rows.append((src, med, camp, sess, conv, users))

    scope = "Google Ads (google / cpc)" if args.google_ads_only else "all channels"
    print(f"# GA4 - {scope} - last {args.days} days (property {prop})\n")
    print("| source | medium | campaign | sessions | conversions | users |")
    print("|---|---|---|---:|---:|---:|")
    for src, med, camp, sess, conv, users in rows:
        print(f"| {src} | {med} | {camp} | {sess} | {conv} | {users} |")
    print(f"\n{len(rows)} rows.")


if __name__ == "__main__":
    main()
