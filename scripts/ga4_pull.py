"""
Step 2 - Direct GA4 (Google Analytics 4) connection.

Authenticates to the Google Analytics Data API with a service-account key and pulls
acquisition data by source / medium / campaign. Isolates the Google Ads rows
(source=google, medium=cpc). Proves the connection returns live data and writes a CSV.

Prerequisites (one-time, you have full admin):
  1. Google Cloud: create/pick a project, enable "Google Analytics Data API".
  2. Create a service account, download its JSON key -> secrets/ga4-service-account.json
  3. GA4 Admin -> Property Access Management -> add the service-account email as Viewer.
  4. Put GA4_PROPERTY_ID (numeric) and GA4_SA_JSON path in .env

Usage:
    python scripts/ga4_pull.py
"""
from __future__ import annotations

import os
import sys
import csv

from dotenv import load_dotenv
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest, OrderBy,
)

load_dotenv()

PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "").strip()
SA_JSON = os.getenv("GA4_SA_JSON", "secrets/ga4-service-account.json").strip()
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def client() -> BetaAnalyticsDataClient:
    if not PROPERTY_ID:
        die("GA4_PROPERTY_ID not set in .env (GA4 Admin -> Property Settings -> Property ID).")
    if not os.path.exists(SA_JSON):
        die(f"Service-account key not found at {SA_JSON}. Download it from Google Cloud and "
            "grant the service-account Viewer on the GA4 property.")
    creds = service_account.Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    return BetaAnalyticsDataClient(credentials=creds)


def run(c: BetaAnalyticsDataClient, days: int) -> list[list[str]]:
    req = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
            Dimension(name="sessionCampaignName"),
        ],
        metrics=[
            Metric(name="sessions"),
            Metric(name="conversions"),  # GA4 aliases this to key events
            Metric(name="totalUsers"),
        ],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="conversions"), desc=True)],
        limit=250,
    )
    resp = c.run_report(req)
    rows = []
    for r in resp.rows:
        dims = [d.value for d in r.dimension_values]
        mets = [m.value for m in r.metric_values]
        rows.append(dims + mets)
    return rows


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    c = client()
    header = ["source", "medium", "campaign", "sessions", "conversions", "users"]

    for days in (30, 90):
        rows = run(c, days)
        out = os.path.join(OUT_DIR, f"ga4_source_medium_{days}d.csv")
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)

        gads = [r for r in rows if r[0].lower() == "google" and r[1].lower() == "cpc"]
        print(f"\n=== GA4 last {days} days (property {PROPERTY_ID}) ===")
        print(f"  rows returned: {len(rows)}  ->  {out}")
        print(f"  Google Ads rows (google / cpc): {len(gads)}")
        for r in gads[:10]:
            print(f"    campaign={r[2]!r:40}  sessions={r[3]:>6}  conversions={r[4]:>6}  users={r[5]:>6}")
        if rows and not gads:
            print("  (No google/cpc rows - check Google Ads <-> GA4 linking, or widen the date range.)")

    print("\nGA4 connection OK - live data returned.")


if __name__ == "__main__":
    main()
