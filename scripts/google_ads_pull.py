"""
Step 3b - Direct Google Ads API connection.

Pulls (a) campaign performance and (b) click-level data including gclid. The gclid
extract is what a FUTURE per-deal match would join against (Google Ads click_view is
limited to the last 90 days by Google).

Prerequisites:
  - A developer token (Google Ads -> Tools -> API Center). NOTE: approval can take days.
  - secrets/google-ads.yaml filled in. Minimum contents:
        developer_token: "..."
        client_id: "...apps.googleusercontent.com"
        client_secret: "..."
        refresh_token: "..."        # from google_ads_auth.py
        login_customer_id: "..."    # MCC id (digits only) if applicable; else same as customer
        use_proto_plus: True
  - GADS_CUSTOMER_ID in .env (digits only, no dashes).

Usage:
    python scripts/google_ads_pull.py
"""
from __future__ import annotations

import os
import sys
import csv

from dotenv import load_dotenv

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
except ImportError:
    print("ERROR: google-ads not installed. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

load_dotenv()

YAML = os.getenv("GADS_YAML", "secrets/google-ads.yaml").strip()
CUSTOMER_ID = os.getenv("GADS_CUSTOMER_ID", "").replace("-", "").strip()
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

CAMPAIGN_GAQL = """
    SELECT campaign.id, campaign.name, campaign.status,
           metrics.impressions, metrics.clicks, metrics.conversions,
           metrics.cost_micros
    FROM campaign
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY metrics.conversions DESC
"""

# click_view carries gclid but can only be queried one day at a time and only for the
# last 90 days. Here we query a single recent day as a connection proof.
CLICK_GAQL = """
    SELECT click_view.gclid, click_view.campaign, segments.date
    FROM click_view
    WHERE segments.date DURING YESTERDAY
"""


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.exists(YAML):
        die(f"{YAML} not found. See the header of this file for required contents.")
    if not CUSTOMER_ID:
        die("GADS_CUSTOMER_ID not set in .env (digits only, no dashes).")

    client = GoogleAdsClient.load_from_storage(YAML)
    ga = client.get_service("GoogleAdsService")

    # --- Campaign performance ---
    try:
        resp = ga.search(customer_id=CUSTOMER_ID, query=CAMPAIGN_GAQL)
    except GoogleAdsException as e:
        die(f"Google Ads API error: {e.error.code().name} - "
            f"{e.failure.errors[0].message if e.failure.errors else e}")

    camp_out = os.path.join(OUT_DIR, "google_ads_campaigns.csv")
    n = 0
    with open(camp_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["campaign_id", "campaign_name", "status", "impressions", "clicks",
                    "conversions", "cost"])
        for row in resp:
            n += 1
            w.writerow([row.campaign.id, row.campaign.name, row.campaign.status.name,
                        row.metrics.impressions, row.metrics.clicks,
                        row.metrics.conversions, row.metrics.cost_micros / 1_000_000])
    print(f"Campaign rows: {n}  ->  {camp_out}")

    # --- Click / gclid extract ---
    click_out = os.path.join(OUT_DIR, "google_ads_clicks.csv")
    m = 0
    try:
        cresp = ga.search(customer_id=CUSTOMER_ID, query=CLICK_GAQL)
        with open(click_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["gclid", "campaign_resource", "date"])
            for row in cresp:
                m += 1
                w.writerow([row.click_view.gclid, row.click_view.campaign, row.segments.date])
        print(f"Click/gclid rows (yesterday): {m}  ->  {click_out}")
    except GoogleAdsException as e:
        print(f"WARN: click_view query failed ({e.error.code().name}). Campaign data still OK.")

    print("\nGoogle Ads connection OK - live data returned.")


if __name__ == "__main__":
    main()
