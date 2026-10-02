# CLAUDE.md — google_ads_leads_tracking

## What this is
Two things: (1) scripts to read GA4 / Google Ads / ActiveCampaign for lead-source
analysis, and (2) the backend worker for the **MAG GA4 Agent** — a dashboard tab
where staff ask a plain-English GA4/Google Ads question and get a live table.

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

## Secrets — never commit (all gitignored)
`service_account_json_key_*.json`, `.env`, `secrets/`, `google-ads.yaml`, and
`*routine*API*Key*.txt` (holds the live fire token). The repo history is clean;
keep it that way.

## Working rules from the user
- Do NOT run git/GitHub commands for THIS repo — the user commits & pushes
  themselves. (Deploying the dashboard via `gcloud` is fine; that's not GitHub.)
