# Google Ads → ActiveCampaign lead/deal attribution

Goal: answer **"which ActiveCampaign deals came from Google Ads?"** from data, not from
AC's manually-tagged fields.

## The key reality
Mapping an ad click to a specific CRM deal needs a shared **`gclid`** (Google Click ID).
AC currently stores **no `gclid`** anywhere, so historical deals can't be matched 1:1.
- **GA4** gives *aggregate* numbers (Google Ads sessions/conversions) — never names a deal.
- **Google Ads API** gives click-level `gclid` data (last 90 days) — the join key for the future.
- Reliable per-deal attribution needs `gclid` captured on web forms into AC going forward.

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then edit .env
```

## Scripts
| Script | What it does | Needs |
|---|---|---|
| `scripts/ac_check_lead_source.py` | **Step 0.** Checks if the Done Digital "Lead Machine Source" fields already flag Google Ads per deal. Writes `output/ac_lead_source_*.csv`. | `AC_API_TOKEN` |
| `scripts/ga4_pull.py` | **Step 2.** Direct GA4 pull by source/medium/campaign; isolates google/cpc. | `GA4_PROPERTY_ID`, service-account key |
| `scripts/google_ads_auth.py` | **Step 3a.** One-time OAuth to mint a refresh token. | Desktop OAuth client JSON |
| `scripts/google_ads_pull.py` | **Step 3b.** Direct Google Ads pull: campaigns + gclid extract. | `secrets/google-ads.yaml`, `GADS_CUSTOMER_ID` |
| `scripts/map_ga4_ac.py` | **Lead mapping.** Matches GA4 form events (by time) + Calendly bookings (by email/UTM) to AC contacts and writes `public.ga4_ac_contact_source` + `ga4_ac_event_matches` in Supabase. | `GA4_*`, `CALENDLY_API_TOKEN[_FILE]`, `SUPABASE_DB_URL` |

### Lead mapping — how it works & its limits
`map_ga4_ac.py` answers "what channel did each lead come from?" two ways:
- **Calendly → AC by email** (reliable): invitee email joins to the AC contact; source is the
  booking's real `utm_*` when present.
- **GA4 form events → AC by timestamp** (probabilistic): GA4's finest grain is the *minute* and
  it returns per-minute event *counts by source*, not identified events — so each AC contact
  inherits the source of the GA4 form event(s) in its creation-minute window. Every GA4 match
  carries a **confidence** (`high` ≤90s & unambiguous · `medium` · `low` when multiple sources
  share the minute). Bulk-import bursts (>2 contacts/minute) are excluded. AC `cdate` is UTC;
  GA4 minutes (property tz Australia/Sydney) are converted to UTC before joining.

The durable fix for exact per-lead attribution is still to capture `gclid`/`utm` on web forms
into AC going forward; this mapping is best-effort for data already collected.

## Credentials needed (you have full admin)
- **ActiveCampaign:** Settings → Developer → API Access → key → `.env` `AC_API_TOKEN`.
- **GA4:** Google Cloud project → enable *Google Analytics Data API* → service account + JSON key
  (`secrets/ga4-service-account.json`) → add that service-account email as **Viewer** on the GA4
  property → put the numeric **Property ID** in `.env`.
- **Google Ads:** enable *Google Ads API* → **developer token** (API Center; approval can take days)
  → Desktop **OAuth client** → run `google_ads_auth.py` → fill `secrets/google-ads.yaml`.

All secrets live in `secrets/` and are git-ignored. Nothing is sent anywhere except Google's
and ActiveCampaign's own APIs.
