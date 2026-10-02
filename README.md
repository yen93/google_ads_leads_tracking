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

## Credentials needed (you have full admin)
- **ActiveCampaign:** Settings → Developer → API Access → key → `.env` `AC_API_TOKEN`.
- **GA4:** Google Cloud project → enable *Google Analytics Data API* → service account + JSON key
  (`secrets/ga4-service-account.json`) → add that service-account email as **Viewer** on the GA4
  property → put the numeric **Property ID** in `.env`.
- **Google Ads:** enable *Google Ads API* → **developer token** (API Center; approval can take days)
  → Desktop **OAuth client** → run `google_ads_auth.py` → fill `secrets/google-ads.yaml`.

All secrets live in `secrets/` and are git-ignored. Nothing is sent anywhere except Google's
and ActiveCampaign's own APIs.
