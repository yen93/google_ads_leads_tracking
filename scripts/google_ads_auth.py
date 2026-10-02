"""
Step 3a - Mint a Google Ads API refresh token (one-time).

The Google Ads API requires OAuth user consent (a service account won't work without
Workspace domain-wide delegation). This runs the desktop consent flow and prints a
refresh token to paste into secrets/google-ads.yaml.

Prerequisites:
  1. Google Cloud: enable "Google Ads API" on your project.
  2. Create an OAuth client of type "Desktop app"; download the JSON
     -> secrets/oauth_client.json  (or set GADS_OAUTH_CLIENT_JSON)
  3. Add yourself as a test user on the OAuth consent screen (if in testing mode).

Usage:
    python scripts/google_ads_auth.py
    # A browser opens; approve; the refresh token is printed.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CLIENT_JSON = os.getenv("GADS_OAUTH_CLIENT_JSON", "secrets/oauth_client.json").strip()
SCOPES = ["https://www.googleapis.com/auth/adwords"]


def main() -> None:
    if not os.path.exists(CLIENT_JSON):
        print(f"ERROR: OAuth client file not found at {CLIENT_JSON}. Create a 'Desktop app' "
              "OAuth client in Google Cloud and download its JSON there.", file=sys.stderr)
        sys.exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_JSON, scopes=SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    print("\n================ COPY THIS ================")
    print(f"refresh_token: {creds.refresh_token}")
    print("Paste it (plus client_id / client_secret / developer_token / customer id) "
          "into secrets/google-ads.yaml")


if __name__ == "__main__":
    main()
