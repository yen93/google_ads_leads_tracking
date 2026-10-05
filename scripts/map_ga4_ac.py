"""
Map GA4 events <-> ActiveCampaign contacts and write the result to Supabase.

Two matching mechanisms, by design:
  * Calendly bookings  -> AC contact by EMAIL (reliable identity; real UTM when the
                          booking link carried utm_* params).
  * GA4 form events    -> AC contact by TIMESTAMP (probabilistic: GA4's finest grain is
                          the minute and it returns per-minute event COUNTS by source,
                          not identified events). Every GA4 match carries a confidence.

Why timestamps work here at all: leads are low-volume (~2/day), so same-minute source
collisions are rare (~2% of minutes). AC `cdate` is stored UTC; GA4 minutes are in the
property's reporting timezone (Australia/Sydney) and are converted to UTC before joining.
Bulk-import bursts (>2 contacts created in the same minute) are excluded from time-matching.

AC contacts are read from Supabase (public.activecampaign_contacts) by the matching SQL;
this script only loads the two staging tables and runs scripts/match_ga4_ac.sql.

Usage:
    python scripts/map_ga4_ac.py --days 60
    python scripts/map_ga4_ac.py --days 60 --tolerance-seconds 300
    python scripts/map_ga4_ac.py --days 60 --dump out/staging.json   # fetch only, no DB

Env (.env):
    GA4_PROPERTY_ID            numeric GA4 property id
    GA4_SA_JSON_B64 | _CONTENT | GA4_SA_JSON   service-account key (as in ga4_routine.py)
    CALENDLY_API_TOKEN         Calendly personal access token (raw), OR
    CALENDLY_API_TOKEN_FILE    path to a file containing the token
    SUPABASE_DB_URL            postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
                               (Supabase -> Settings -> Database -> Connection string)
"""
from __future__ import annotations

import os
import sys
import json
import time
import base64
import argparse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import requests
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest, Filter, FilterExpression,
)

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
HERE = os.path.dirname(os.path.abspath(__file__))
MATCH_SQL = os.path.join(HERE, "match_ga4_ac.sql")

# GA4 events treated as "form submits". Calendly-specific events are intentionally
# EXCLUDED here (Calendly is sourced from its own API by email) to avoid double counting.
DEFAULT_FORM_EVENTS = [
    "form_submit", "form_submissions", "Contact_Form_Submit_Alt",
    "Form_submission_thankyou_1", "subscribe", "free_gifts_opt_ins",
]
CALENDLY_GA4_EVENTS = ["consult_booking_calendly", "calendly_form_submit", "executive_x_bookings"]


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --- GA4 ------------------------------------------------------------------------------

def ga4_credentials():
    b64 = os.getenv("GA4_SA_JSON_B64", "").strip()
    if b64:
        return service_account.Credentials.from_service_account_info(
            json.loads(base64.b64decode(b64)), scopes=SCOPES)
    inline = os.getenv("GA4_SA_JSON_CONTENT", "").strip()
    if inline:
        return service_account.Credentials.from_service_account_info(
            json.loads(inline), scopes=SCOPES)
    path = os.getenv("GA4_SA_JSON", "").strip()
    if path and os.path.exists(path):
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    die("No GA4 service-account key (set GA4_SA_JSON_B64 / _CONTENT / GA4_SA_JSON).")


def fetch_ga4_events(days: int, events: list[str]) -> list[dict]:
    prop = os.getenv("GA4_PROPERTY_ID", "").strip()
    if not prop:
        die("GA4_PROPERTY_ID not set.")
    client = BetaAnalyticsDataClient(credentials=ga4_credentials())
    req = RunReportRequest(
        property=f"properties/{prop}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="dateHourMinute"), Dimension(name="eventName"),
                    Dimension(name="sessionSource"), Dimension(name="sessionMedium"),
                    Dimension(name="sessionCampaignName")],
        metrics=[Metric(name="eventCount")],
        dimension_filter=FilterExpression(filter=Filter(
            field_name="eventName", in_list_filter=Filter.InListFilter(values=events))),
        limit=100000,
    )
    resp = client.run_report(req)
    tz = ZoneInfo(resp.metadata.time_zone or "UTC")   # property reporting timezone
    out = []
    for r in resp.rows:
        dhm = r.dimension_values[0].value  # YYYYMMDDHHMM in property tz
        local = datetime(int(dhm[0:4]), int(dhm[4:6]), int(dhm[6:8]),
                         int(dhm[8:10]), int(dhm[10:12]), tzinfo=tz)
        out.append({
            "event_time_utc": local.astimezone(timezone.utc).isoformat(),
            "event_name": r.dimension_values[1].value,
            "source": r.dimension_values[2].value,
            "medium": r.dimension_values[3].value,
            "campaign": r.dimension_values[4].value,
            "event_count": int(r.metric_values[0].value),
        })
    print(f"GA4: {len(out)} form minute-rows (tz={resp.metadata.time_zone}, last {days}d)")
    return out


# --- Calendly -------------------------------------------------------------------------

def calendly_token() -> str:
    tok = os.getenv("CALENDLY_API_TOKEN", "").strip()
    if tok:
        return tok
    path = os.getenv("CALENDLY_API_TOKEN_FILE", "").strip()
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    die("No Calendly token (set CALENDLY_API_TOKEN or CALENDLY_API_TOKEN_FILE).")


def fetch_calendly_bookings(days: int) -> list[dict]:
    token = calendly_token()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = "https://api.calendly.com"
    me = requests.get(f"{base}/users/me", headers=h, timeout=30)
    me.raise_for_status()
    org = me.json()["resource"]["current_organization"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out: list[dict] = []
    url = f"{base}/scheduled_events"
    params = {"organization": org, "min_start_time": since, "count": 100}
    while url:
        r = requests.get(url, headers=h, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2); continue
        r.raise_for_status()
        data = r.json()
        for ev in data.get("collection", []):
            inv = requests.get(f"{ev['uri']}/invitees", headers=h,
                               params={"count": 100}, timeout=30)
            inv.raise_for_status()
            for i in inv.json().get("collection", []):
                t = i.get("tracking") or {}
                out.append({
                    "email": (i.get("email") or "").strip().lower(),
                    "booking_created_at": i.get("created_at") or ev.get("created_at"),
                    "start_time": ev.get("start_time"),
                    "utm_source": t.get("utm_source"),
                    "utm_medium": t.get("utm_medium"),
                    "utm_campaign": t.get("utm_campaign"),
                    "event_name": ev.get("name"),
                })
            time.sleep(0.2)
        nxt = data.get("pagination", {}).get("next_page")
        url, params = (nxt, None) if nxt else (None, None)
    print(f"Calendly: {len(out)} invitees across bookings (last {days}d)")
    return out


# --- Supabase write + match -----------------------------------------------------------

def load_match_sql(days: int, tol_seconds: int) -> list[str]:
    raw = open(MATCH_SQL, encoding="utf-8").read()
    raw = raw.replace("{{WINDOW_DAYS}}", str(int(days)))
    raw = raw.replace("{{TOL_SECONDS}}", str(int(tol_seconds)))
    # split on marker lines beginning with "-- @@"
    stmts, buf = [], []
    for line in raw.splitlines():
        if line.startswith("-- @@"):
            if buf:
                stmts.append("\n".join(buf)); buf = []
        else:
            buf.append(line)
    if buf:
        stmts.append("\n".join(buf))
    return [s.strip() for s in stmts if s.strip() and not s.strip().startswith("--")]


def write_to_supabase(ga4: list[dict], cal: list[dict], days: int, tol_seconds: int) -> None:
    import psycopg
    dsn = os.getenv("SUPABASE_DB_URL", "").strip()
    if not dsn:
        die("SUPABASE_DB_URL not set (Supabase -> Settings -> Database -> Connection string).")
    with psycopg.connect(dsn, sslmode="require", autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate public.ga4_events_staging")
            cur.execute("truncate public.calendly_bookings_staging")
            if ga4:
                cur.executemany(
                    "insert into public.ga4_events_staging "
                    "(event_time_utc,event_name,source,medium,campaign,event_count) "
                    "values (%(event_time_utc)s,%(event_name)s,%(source)s,%(medium)s,"
                    "%(campaign)s,%(event_count)s)", ga4)
            if cal:
                cur.executemany(
                    "insert into public.calendly_bookings_staging "
                    "(email,booking_created_at,start_time,utm_source,utm_medium,"
                    "utm_campaign,event_name) values (%(email)s,%(booking_created_at)s,"
                    "%(start_time)s,%(utm_source)s,%(utm_medium)s,%(utm_campaign)s,"
                    "%(event_name)s)", cal)
            for stmt in load_match_sql(days, tol_seconds):
                cur.execute(stmt)
        conn.commit()
    print("Supabase: staging loaded and mapping rebuilt.")


# --- main -----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Map GA4 events <-> AC contacts into Supabase.")
    ap.add_argument("--days", type=int, default=60,
                    help="look-back window (default 60; GA4 minute data ~2 months retention)")
    ap.add_argument("--tolerance-seconds", type=int, default=300,
                    help="GA4 time-match window around a contact's cdate (default 300)")
    ap.add_argument("--dump", metavar="PATH",
                    help="write fetched staging data to JSON and skip the DB write")
    ap.add_argument("--events", nargs="*", default=DEFAULT_FORM_EVENTS,
                    help="GA4 event names to treat as form submits")
    args = ap.parse_args()

    ga4 = fetch_ga4_events(args.days, args.events)
    cal = fetch_calendly_bookings(args.days)

    if args.dump:
        os.makedirs(os.path.dirname(os.path.abspath(args.dump)), exist_ok=True)
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump({"ga4": ga4, "calendly": cal,
                       "days": args.days, "tolerance_seconds": args.tolerance_seconds},
                      fh, indent=2)
        print(f"Wrote staging JSON -> {args.dump} (no DB write).")
        return

    write_to_supabase(ga4, cal, args.days, args.tolerance_seconds)


if __name__ == "__main__":
    main()
