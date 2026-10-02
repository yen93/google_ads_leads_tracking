"""
Step 0 - Quick win: does ActiveCampaign already identify Google Ads leads per deal?

Pulls every deal, joins each deal to its primary contact's automated source fields
(created by "Done Digital Lead Machine") and reports whether those fields already
carry a Google Ads signature (google / cpc / paid / adwords / gclid). Also compares
against the MANUAL deal field "Google Ads?" so you can see how far the manual tagging
diverges from the automated data.

Reads data only. Writes two CSVs to output/. Needs an AC API token (read-only use).

Usage:
    pip install -r requirements.txt
    # put AC_ACCOUNT + AC_API_TOKEN in .env  (copy .env.example)
    python scripts/ac_check_lead_source.py
"""
from __future__ import annotations

import os
import sys
import time
import csv
import re
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

ACCOUNT = os.getenv("AC_ACCOUNT", "").strip()
TOKEN = os.getenv("AC_API_TOKEN", "").strip()
# AC's API host uses the account SUBDOMAIN name, not the numeric id. Prefer the full URL
# shown in AC -> Settings -> Developer -> API Access (e.g. https://myaccount.api-us1.com).
API_URL = os.getenv("AC_API_URL", "").strip().rstrip("/")
BASE = (f"{API_URL}/api/3" if API_URL else f"https://{ACCOUNT}.api-us1.com/api/3")

# Field ids discovered during planning (AC account 2116665)
F_LM_SOURCE_FIRST = "63"   # "Lead Machine Source First"  (contact custom field)
F_LM_SOURCE_LAST = "64"    # "Lead Machine Source Last"   (contact custom field)
F_LEAD_SOURCE = "20"       # "Lead Source" dropdown       (contact custom field)
DEAL_F_GOOGLE_ADS = "18"   # "Google Ads?" Yes/No         (deal custom field, MANUAL)

# What counts as a "Google Ads" signature in a free-text source value.
GADS_PATTERN = re.compile(r"(google.*(cpc|ppc|paid|ads|adwords)|gclid|adwords)", re.I)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def session() -> requests.Session:
    if not TOKEN:
        die("AC_API_TOKEN is not set. Copy .env.example to .env and add your token "
            "(AC -> Settings -> Developer -> API Access).")
    s = requests.Session()
    s.headers.update({"Api-Token": TOKEN, "Accept": "application/json"})
    return s


def get_all(s: requests.Session, path: str, root_key: str, params: dict | None = None,
            page_size: int = 100, hard_cap: int = 100_000) -> list[dict]:
    """Page through an AC list endpoint and return all records under root_key."""
    params = dict(params or {})
    params["limit"] = page_size
    out: list[dict] = []
    offset = 0
    while True:
        params["offset"] = offset
        r = s.get(f"{BASE}/{path}", params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(1.0)
            continue
        r.raise_for_status()
        data = r.json()
        batch = data.get(root_key, [])
        out.extend(batch)
        total = int(data.get("meta", {}).get("total", len(out)))
        offset += page_size
        if offset >= total or not batch or len(out) >= hard_cap:
            break
        time.sleep(0.25)  # stay well under AC's 5 req/sec limit
    return out


def field_value_map(s: requests.Session, field_id: str) -> dict[str, str]:
    """contact_id -> value, for one contact custom field, via /fieldValues?filters[fieldid]."""
    rows = get_all(s, "fieldValues", "fieldValues", params={"filters[fieldid]": field_id})
    # Guard: the filter is honoured server-side, but double-check client-side too.
    return {str(r["contact"]): (r.get("value") or "").strip()
            for r in rows if str(r.get("field")) == field_id}


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    s = session()

    print("Pulling deals ...")
    deals = get_all(s, "deals", "deals")
    print(f"  {len(deals)} deals")

    print("Pulling automated source fields (Done Digital Lead Machine) ...")
    lm_first = field_value_map(s, F_LM_SOURCE_FIRST)
    lm_last = field_value_map(s, F_LM_SOURCE_LAST)
    lead_source = field_value_map(s, F_LEAD_SOURCE)
    print(f"  LM_SOURCE_FIRST populated on {sum(1 for v in lm_first.values() if v)} contacts")
    print(f"  LM_SOURCE_LAST  populated on {sum(1 for v in lm_last.values() if v)} contacts")

    print("Pulling manual deal field 'Google Ads?' for comparison ...")
    try:
        rows_g = get_all(s, f"dealCustomFieldMeta/{DEAL_F_GOOGLE_ADS}/dealCustomFieldData",
                         "dealCustomFieldData")
        deal_gads = {str(r.get("dealId") or r.get("deal")): (r.get("fieldValue") or "").strip()
                     for r in rows_g}
    except Exception as e:
        print(f"  (skipped manual-field comparison: {e})")
        deal_gads = {}

    # Build per-deal rows
    rows = []
    first_counter: Counter = Counter()
    last_counter: Counter = Counter()
    gads_auto_deals = []
    agree = disagree = 0

    for d in deals:
        cid = str(d.get("contact") or "")
        f = lm_first.get(cid, "")
        l = lm_last.get(cid, "")
        first_counter[f or "(blank)"] += 1
        last_counter[l or "(blank)"] += 1
        auto_is_gads = bool(GADS_PATTERN.search(f) or GADS_PATTERN.search(l))
        manual_is_gads = deal_gads.get(str(d.get("id")), "").lower() in ("yes", "1", "true")
        if auto_is_gads:
            gads_auto_deals.append(d)
        if auto_is_gads == manual_is_gads:
            agree += 1
        else:
            disagree += 1
        rows.append({
            "deal_id": d.get("id"),
            "deal_title": d.get("title"),
            "deal_value": d.get("value"),
            "deal_status": d.get("status"),   # 0 open, 1 won, 2 lost
            "stage_id": d.get("stage"),
            "contact_id": cid,
            "lm_source_first": f,
            "lm_source_last": l,
            "contact_lead_source_id": lead_source.get(cid, ""),
            "auto_flag_google_ads": auto_is_gads,
            "manual_field_google_ads": deal_gads.get(str(d.get("id")), ""),
        })

    # Write the per-deal CSV
    by_deal = os.path.join(OUT_DIR, "ac_lead_source_by_deal.csv")
    with open(by_deal, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)

    # Write the distinct-values summary
    summ = os.path.join(OUT_DIR, "ac_lead_source_values.csv")
    with open(summ, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "value", "deal_count"])
        for v, n in first_counter.most_common():
            w.writerow(["LM_SOURCE_FIRST", v, n])
        for v, n in last_counter.most_common():
            w.writerow(["LM_SOURCE_LAST", v, n])

    # Console verdict
    print("\n================ VERDICT ================")
    print(f"Deals total:                         {len(deals)}")
    print(f"Deals flagged Google Ads by AUTO:    {len(gads_auto_deals)}")
    print(f"Agreement auto-vs-manual:            {agree}  |  disagreement: {disagree}")
    print("\nTop LM_SOURCE_FIRST values across deals:")
    for v, n in first_counter.most_common(15):
        tag = "  <-- Google Ads" if GADS_PATTERN.search(v) else ""
        print(f"  {n:6d}  {v}{tag}")
    print(f"\nWrote: {by_deal}")
    print(f"Wrote: {summ}")
    if not any(GADS_PATTERN.search(v) for v in first_counter) and \
       not any(GADS_PATTERN.search(v) for v in last_counter):
        print("\nNOTE: No Google Ads signature found in the automated source fields. "
              "They are either blank or do not carry channel data -> the GA4/Google Ads "
              "API connection (Steps 2-3) is the way forward.")


if __name__ == "__main__":
    main()
