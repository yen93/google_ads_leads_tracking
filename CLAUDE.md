# CLAUDE.md — google_ads_leads_tracking

## What this is
Three things: (1) scripts to read GA4 / Google Ads / ActiveCampaign for lead-source
analysis, (2) the backend worker for the **MAG GA4 Agent** — a dashboard tab
where staff ask a plain-English GA4/Google Ads question and get a live table, and
(3) the **lead-source mapping** that powers the dashboard's **Lead Source Tracking**
tab (per-AC-contact channel + conversion action).

Core finding (don't re-litigate): per-deal "which deal came from Google Ads"
attribution is **not possible from current data** — AC stores no `gclid`/`utm`
anywhere, and its "Google Ads?" / "Lead Source" fields are manual and unused.
GA4 gives aggregate Google Ads volume only. Reliable per-deal attribution would
require capturing `gclid` on web forms into AC, going forward.

## Setup / run
- Python 3.14 venv at `.venv` (`./.venv/Scripts/python.exe` on this Windows box).
- `pip install -r requirements.txt`. Config in `.env` (copy `.env.example`).
- Local GA4 report: `python scripts/ga4_pull.py`. AC audit: `python scripts/ac_check_lead_source.py`.
- The cloud routine runs `scripts/ga4_routine.py --days N [--google-ads-only] [--limit N]`.

## Key IDs
- GCP project `claudegwscli-502400`; GA4 property `363754280`; service account
  `insta-drive-uploader@claudegwscli-502400.iam.gserviceaccount.com` (Viewer on GA4).
- AC account 2116665; **API host `https://myadventuregroup.api-us1.com`** (the
  account SUBDOMAIN, NOT the numeric id — the numeric id 404s).
- Supabase project `aivitcomiywiysrfwqxt` (MAGTestProject); table
  `public.ga4_agent_requests`.
- Routine "GA4 On-Demand Report" `trig_01DFkHgvzKJUDCNcHwM87gyS`; cloud env
  `env_01DYHNjMesGeuq9ABo7W6m6f`.
- Dashboard = Cloud Run service `dashboard`, project `claudegwscli-502400`,
  region `australia-southeast1`. Its code is a SEPARATE repo at
  `C:/Users/Cloverly/claude_code/dashboard` (mag-metrics-dashboard).

## GA4 Agent architecture (how a question flows)
dashboard tab → `POST /api/ga4-agent/run` (server/ga4Agent.js) inserts a `pending`
row in `ga4_agent_requests` and fires the routine's `/fire` endpoint (token
server-side) → routine claims the oldest pending row, runs `ga4_routine.py`,
writes the markdown result back (`done`) via Supabase MCP → tab polls
`GET /api/ga4-agent/result?id=` until done. Mirrors the dashboard's existing
"routine writes a Supabase table, dashboard reads it" pattern (sales-topline-sync).

## Lead source mapping (GA4/Calendly → AC → "Lead Source Tracking" tab)
- `scripts/map_ga4_ac.py` (+ `match_ga4_ac.sql`) builds the 60-day per-contact source
  cache `public.ga4_ac_contact_source`: Calendly→AC by **email** (exact), GA4 form
  events→AC by **timestamp** (±300s, confidence-scored high/medium/low).
- `scripts/map_t1_enquiries.py` (+ `match_t1_ac.sql`) is a T1-scoped variant that
  reproduces the Data Studio "27 T1 Enquiries by channel" (the 4 T1 events, last 30d)
  into `public.t1_enquiry_channels` / `t1_enquiry_contacts`. `click_to_call` /
  `email_link_clicks` are anonymous (no contact) — only form + Calendly leads map.
- The dashboard's **Lead Source Tracking** tab (sibling repo; file still
  `public/ac_contact_sources.html`, was "AC Contact Sources") reads
  `ga4_ac_contact_source` and shows a **Channel** + **Conversion Action** column with
  tooltips linking to `public/lead_source_guide.html`. The `channel` column is the full
  GA4 source/medium per contact; for Calendly rows it's derived from the GA4
  `calendly_form_submit` session via `scripts/ad_search_source.py`.
- How the dashboard's "by channel" figures are derived: GA4 session source/medium ×
  the 4 T1 events, last 30d (documented in `data_studio/channel_breakdown_findings.txt`).

## GOTCHAS (these bit us — read before touching the GA4 Agent)
- **NEVER edit the routine in the claude.ai UI.** Saving it there reverts the
  worker prompt to the original and drops `mcp__Supabase__execute_sql` from
  allowed_tools. Manage the routine ONLY via the RemoteTrigger API (action
  "update"). `/fire` uses the routine's LIVE config (not a token snapshot).
- The routine's `allowed_tools` must include `mcp__Supabase__execute_sql`
  (and the Supabase MCP connector must be attached) or it can't write results.
- The `/fire` request MUST send header `anthropic-version: 2023-06-01` (else 400).
- The routine claims the **oldest pending** row. Keep the table free of stale
  non-done rows (`delete from public.ga4_agent_requests where status<>'done'`) or
  a new submit polls an id that won't be the one processed.
- GA4 Data API: `conversions` and `keyEvents` are duplicate metrics — request
  only one.
- Service-account key is passed to the cloud routine as **base64** in env var
  `GA4_SA_JSON_B64` (the cloud env-var UI only accepts single-line KEY=value).
- Cloud Run env vars: add with `gcloud run services update … --update-env-vars`
  (ADDITIVE); never `--set-env-vars` (it wipes the other vars). Deploy code with
  `gcloud run deploy dashboard --source . --region australia-southeast1`.

## GOTCHAS (lead source mapping)
- **`SUPABASE_DB_URL` is blank in `.env` and the direct Postgres URL won't connect from
  this box.** Run the mappers with `--dump out/x.json` to fetch, then load staging +
  run the match SQL via the **Supabase MCP** (`mcp__Supabase__execute_sql`) — not psycopg.
- **`ga4_ac_contact_source.channel` and the T1 tables are populated OUT-OF-BAND via MCP.**
  Re-running `map_ga4_ac.py` rebuilds `ga4_ac_contact_source` and NULLs `channel` →
  re-enrich: UPDATE `ga4_time` rows' channel from source/medium, run
  `scripts/ad_search_source.py`, UPDATE `calendly_email` rows from its JSON. (TODO: fold
  into `match_ga4_ac.sql`.)
- GA4 minute-grain data has ~2-month retention — keep `--days` ≤ ~60.
- Google Ads API is still blocked: customer id 402-451-5888 (`GADS_CUSTOMER_ID` set in
  `.env`) but the owner login is Billing-only with no manager account, so no dev token.

## Secrets — never commit (all gitignored)
`service_account_json_key_*.json`, `.env`, `secrets/`, `google-ads.yaml`, and
`*routine*API*Key*.txt` (holds the live fire token). The repo history is clean;
keep it that way.

## Working rules from the user
- Do NOT run git/GitHub commands for THIS repo — the user commits & pushes
  themselves. (Deploying the dashboard via `gcloud` is fine; that's not GitHub.)
