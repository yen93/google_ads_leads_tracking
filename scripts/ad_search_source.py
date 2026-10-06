"""Derive the GA4 ad/search channel for CALENDLY contacts and emit email->channel.

The main cache (public.ga4_ac_contact_source) stores Calendly contacts with
source='(calendly)'/medium='booking' (from the Calendly API), which hides the ad
channel. This pulls the GA4 calendly_form_submit events + Calendly bookings, matches
each booking to its GA4 event by timestamp (+-tolerance), and classifies the GA4
session source/medium into one of: 'google / cpc', 'bing / cpc', 'google / organic'.

Output: JSON [{email, ad_search_source}] for contacts whose Calendly booking mapped to
one of those three channels. Load it into ga4_ac_contact_source.ad_search_source
(match_method='calendly_email') by email. Keeps the old source/medium columns intact.

Usage:
    python scripts/ad_search_source.py --days 60 --out out/ad_search_source.json
"""
from __future__ import annotations

import os, sys, json, argparse
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_ga4_ac import fetch_ga4_events, fetch_calendly_bookings

TOL = 300  # seconds between a Calendly booking and its GA4 calendly_form_submit event


def channel(source: str, medium: str) -> str:
    """Full session channel string, e.g. 'google / cpc', '(direct) / (none)'."""
    return f"{source or '(not set)'} / {medium or '(none)'}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out", default="out/ad_search_source.json")
    args = ap.parse_args()

    ga4 = fetch_ga4_events(args.days, ["calendly_form_submit"])
    cal = fetch_calendly_bookings(args.days)

    # GA4 calendly events as (utc datetime, source, medium)
    events = []
    for g in ga4:
        events.append((datetime.fromisoformat(g["event_time_utc"]), g["source"], g["medium"]))

    # For each booking, find nearest GA4 calendly event within tolerance -> classify.
    best_by_email: dict[str, tuple[datetime, str]] = {}
    for b in cal:
        email = (b.get("email") or "").strip().lower()
        if not email or not b.get("booking_created_at"):
            continue
        bt = datetime.fromisoformat(str(b["booking_created_at"]).replace("Z", "+00:00"))
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        near, ndelta = None, None
        for et, src, med in events:
            d = abs((bt - et).total_seconds())
            if d <= TOL and (ndelta is None or d < ndelta):
                near, ndelta = (src, med), d
        if not near:
            continue
        label = channel(*near)
        # most recent booking with a GA4-matched channel wins per email
        if email not in best_by_email or bt > best_by_email[email][0]:
            best_by_email[email] = (bt, label)

    out = [{"email": e, "channel": v[1]} for e, v in best_by_email.items()]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    counts = defaultdict(int)
    for r in out:
        counts[r["channel"]] += 1
    print(f"\nCalendly contacts classified: {len(out)}")
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k} = {n}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
