"""
Map the dashboard's "T1 Enquiries by channel" figures to ActiveCampaign contacts.

"Total T1 Enquiries" on the MAG Data Studio dashboard is the sum of FOUR GA4 custom
key events (last 30 days):

    calendly_form_submit        -> creates an AC contact (identity = EMAIL, reliable)
    Form_submission_thankyou_1  -> creates an AC contact (identity = TIMESTAMP, probabilistic)
    click_to_call               -> anonymous tel: click  (NO identity -> unmappable)
    email_link_clicks           -> anonymous mailto: click (NO identity -> unmappable)

So only the first two can be tied to an AC contact. The phone/email clicks carry no
email and create no contact, so they are counted per channel as "unmappable", never
invented into contacts.

This reuses the generic GA4<->AC pipeline's building blocks (scripts/map_ga4_ac.py)
but scopes the GA4 pull to the 4 T1 events and runs scripts/match_t1_ac.sql, writing
two Supabase tables: public.t1_enquiry_contacts and public.t1_enquiry_channels.

Usage:
    python scripts/map_t1_enquiries.py --days 30
    python scripts/map_t1_enquiries.py --days 30 --dump out/t1_staging.json   # validate only, no DB
    python scripts/map_t1_enquiries.py --days 30 --tolerance-seconds 300

Env (.env): same as map_ga4_ac.py (GA4_*, CALENDLY_API_TOKEN[_FILE], SUPABASE_DB_URL).
"""
from __future__ import annotations

import os
import sys
import json
import argparse
from collections import defaultdict

# Reuse the existing pipeline's building blocks (same directory on sys.path[0]).
from map_ga4_ac import (
    fetch_ga4_events, fetch_calendly_bookings, die,
)

HERE = os.path.dirname(os.path.abspath(__file__))
MATCH_SQL = os.path.join(HERE, "match_t1_ac.sql")

# The four conversion actions that make up "Total T1 Enquiries" on the dashboard.
T1_EVENTS = [
    "calendly_form_submit",
    "Form_submission_thankyou_1",
    "click_to_call",
    "email_link_clicks",
]
IDENTITY_EVENTS = {"calendly_form_submit", "Form_submission_thankyou_1"}


def summarise(ga4: list[dict]) -> None:
    """Print the by-action and by-channel breakdowns so they can be checked against
    the dashboard (should total 27 for the last-30-days window)."""
    by_action: dict[str, int] = defaultdict(int)
    by_channel: dict[tuple[str, str], int] = defaultdict(int)
    for r in ga4:
        by_action[r["event_name"]] += r["event_count"]
        by_channel[(r["source"], r["medium"])] += r["event_count"]
    total = sum(by_action.values())

    print("\n== T1 Enquiries by conversion action ==")
    for name in T1_EVENTS:
        mappable = "identity" if name in IDENTITY_EVENTS else "ANONYMOUS (unmappable)"
        print(f"  {name:<28} {by_action.get(name, 0):>3}   [{mappable}]")
    print(f"  {'TOTAL':<28} {total:>3}")

    print("\n== T1 Enquiries by channel (sessionSource / sessionMedium) ==")
    for (src, med), n in sorted(by_channel.items(), key=lambda kv: -kv[1]):
        print(f"  {src} / {med} = {n}")
    print(f"  Grand total = {total}")

    anon = sum(v for k, v in by_action.items() if k not in IDENTITY_EVENTS)
    print(f"\nMappable to an AC contact at best: {total - anon} "
          f"(calendly + form); structurally unmappable: {anon} (phone/email clicks).")


# --- Supabase write (mirrors map_ga4_ac.write_to_supabase, but runs match_t1_ac.sql) ---

def load_match_sql(days: int, tol_seconds: int) -> list[str]:
    raw = open(MATCH_SQL, encoding="utf-8").read()
    raw = raw.replace("{{WINDOW_DAYS}}", str(int(days)))
    raw = raw.replace("{{TOL_SECONDS}}", str(int(tol_seconds)))
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
    print("Supabase: staging loaded, T1 mapping rebuilt "
          "(public.t1_enquiry_contacts + public.t1_enquiry_channels).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Map T1 enquiries by channel -> AC contacts.")
    ap.add_argument("--days", type=int, default=30,
                    help="look-back window (default 30, matching the dashboard view)")
    ap.add_argument("--tolerance-seconds", type=int, default=300,
                    help="time-match window around a contact's cdate (default 300)")
    ap.add_argument("--dump", metavar="PATH",
                    help="write fetched staging data to JSON and skip the DB write "
                         "(use to validate the GA4 totals against the dashboard first)")
    args = ap.parse_args()

    ga4 = fetch_ga4_events(args.days, T1_EVENTS)
    summarise(ga4)
    cal = fetch_calendly_bookings(args.days)

    if args.dump:
        os.makedirs(os.path.dirname(os.path.abspath(args.dump)), exist_ok=True)
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump({"ga4": ga4, "calendly": cal,
                       "days": args.days, "tolerance_seconds": args.tolerance_seconds},
                      fh, indent=2)
        print(f"\nWrote staging JSON -> {args.dump} (no DB write).")
        return

    write_to_supabase(ga4, cal, args.days, args.tolerance_seconds)


if __name__ == "__main__":
    main()
