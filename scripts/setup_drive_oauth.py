#!/usr/bin/env python3
"""
scripts/setup_drive_oauth.py
One-step OAuth 2.0 setup script for Google Drive delivery pipeline.
Authorizes admin@trustbridgevn.com with Google Drive scope.

Supports:
1. Google Cloud SDK CLI (zero configuration needed):
   Runs: ~/Downloads/Misc/google-cloud-sdk/bin/gcloud auth login admin@trustbridgevn.com --enable-gdrive-access
   and automatically extracts refresh token, client ID, and secret into .env and token.json.
2. Local Desktop / Redirect Server Flow:
   Spins up local server (http://localhost:8085), prints authorization URL, and handles callback.
3. Manual token entry / extraction from gcloud database.
4. Token verification test.
"""
import os
import sys
import json
import shutil
import sqlite3
import argparse
import subprocess
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any, Tuple

# Ensure project root is in path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_ACCOUNT = "admin@trustbridgevn.com"
DEFAULT_PORT = 8085
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"

GCLOUD_CANDIDATES = [
    os.path.expanduser("~/Downloads/Misc/google-cloud-sdk/bin/gcloud"),
    shutil.which("gcloud"),
    "/usr/local/bin/gcloud",
    "/usr/bin/gcloud",
]


def find_gcloud() -> Optional[str]:
    for p in GCLOUD_CANDIDATES:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def extract_gcloud_credentials(account: str) -> Optional[Dict[str, Any]]:
    """Extracts credentials stored in gcloud SQLite credentials.db for the account."""
    db_path = os.path.expanduser("~/.config/gcloud/credentials.db")
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT account_id, value FROM credentials WHERE account_id = ?", (account,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT account_id, value FROM credentials WHERE LOWER(account_id) = ?", (account.lower(),))
            row = cur.fetchone()
        if row:
            val = row[1]
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="ignore")
            data = json.loads(val)
            return {
                "account": row[0],
                "refresh_token": data.get("refresh_token"),
                "client_id": data.get("client_id"),
                "client_secret": data.get("client_secret"),
                "scopes": data.get("scopes", DRIVE_SCOPES),
            }
    except Exception as e:
        print(f"[-] Could not read gcloud credentials.db: {e}")
    return None


def update_env_file(env_path: str, updates: Dict[str, str]) -> None:
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                updated_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in updates.items():
        if k not in updated_keys:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def save_tokens(
    account: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    access_token: Optional[str] = None,
):
    # 1. token.json
    token_file = os.path.join(PROJECT_ROOT, "token.json")
    token_data = {
        "account": account,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "access_token": access_token or "",
        "token_uri": TOKEN_URI,
        "scopes": DRIVE_SCOPES,
    }
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)
    print(f"[+] Saved credentials to {token_file}")

    # 2. .env
    env_file = os.path.join(PROJECT_ROOT, ".env")
    env_updates = {
        "GOOGLE_ADMIN_EMAIL": account,
        "GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token,
        "GOOGLE_DRIVE_CLIENT_ID": client_id,
        "GOOGLE_DRIVE_CLIENT_SECRET": client_secret,
        "GOOGLE_SERVICE_ACCOUNT_JSON": "",  # Clear any failed service account JSON
    }
    if access_token:
        env_updates["GOOGLE_DRIVE_ACCESS_TOKEN"] = access_token
    update_env_file(env_file, env_updates)
    print(f"[+] Updated OAuth variables in {env_file}")


def verify_token(refresh_token: str, client_id: str, client_secret: str) -> bool:
    import httpx
    print("[*] Verifying OAuth credentials with Google API endpoint...")
    try:
        resp = httpx.post(
            TOKEN_URI,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15.0,
        )
        if resp.status_code == 200:
            token_json = resp.json()
            access_token = token_json.get("access_token")
            print(f"[+] Successfully refreshed access token! (prefix: {access_token[:15]}...)")
            drive_resp = httpx.get(
                "https://www.googleapis.com/drive/v3/about?fields=user",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15.0,
            )
            if drive_resp.status_code == 200:
                user_info = drive_resp.json().get("user", {})
                email = user_info.get("emailAddress", "unknown")
                name = user_info.get("displayName", "")
                print(f"[+] Google Drive access verified! Account: {name} <{email}> ✅")
                return True
            else:
                print(f"[!] Drive API query status {drive_resp.status_code}: {drive_resp.text}")
                return True
        else:
            print(f"[-] Token refresh returned {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"[-] Error verifying token: {e}")
        return False


def run_gcloud_flow(account: str) -> bool:
    gcloud = find_gcloud()
    if not gcloud:
        print("[-] gcloud CLI not found on this system.")
        return False

    cmd = [gcloud, "auth", "login", account, "--enable-gdrive-access"]
    print(f"[*] Running: {' '.join(cmd)}")
    print("[*] Complete authorization in your browser window...")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"[-] gcloud auth login failed (exit code {res.returncode})")
        return False

    print("[*] Extracting credentials from gcloud database...")
    creds = extract_gcloud_credentials(account)
    if not creds or not creds.get("refresh_token"):
        print(f"[-] Could not find refresh token in gcloud database for {account}.")
        print("[*] Trying fallback: running 'gcloud auth print-access-token'...")
        tok_res = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True)
        if tok_res.returncode == 0 and tok_res.stdout.strip():
            acc_tok = tok_res.stdout.strip()
            save_tokens(account, "", "", "", access_token=acc_tok)
            return True
        return False

    save_tokens(
        account=creds.get("account", account),
        client_id=creds.get("client_id", ""),
        client_secret=creds.get("client_secret", ""),
        refresh_token=creds.get("refresh_token", ""),
    )
    verify_token(creds["refresh_token"], creds.get("client_id", ""), creds.get("client_secret", ""))
    return True


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
            <html><body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h2 style="color: #2e7d32;">Authentication Successful!</h2>
                <p>Google Drive authorization code received. You can now close this tab and return to your terminal.</p>
            </body></html>
            """)
        else:
            OAuthCallbackHandler.error = params.get("error", ["Unknown error"])[0]
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Authentication Failed</h2></body></html>")

    def log_message(self, format, *args):
        pass


def run_desktop_flow(client_id: str, client_secret: str, account: str, port: int = DEFAULT_PORT) -> bool:
    import httpx
    redirect_uri = f"http://localhost:{port}"
    scope_str = " ".join(DRIVE_SCOPES)
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope_str,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": account,
    }
    auth_url = f"{AUTH_URI}?{urllib.parse.urlencode(auth_params)}"

    print("\n" + "=" * 70)
    print("GOOGLE DRIVE 1-CLICK AUTHORIZATION")
    print("=" * 70)
    print(f"\nPlease open the following URL in your web browser to authorize {account}:\n")
    print(auth_url)
    print("\n" + "=" * 70 + "\n")

    # Start local redirect server
    server = None
    try:
        server = HTTPServer(("localhost", port), OAuthCallbackHandler)
        server.timeout = 180
        print(f"[*] Waiting for browser callback on {redirect_uri} (timeout 180s)...")
        server.handle_request()
    except Exception as e:
        print(f"[-] Could not start local server on port {port}: {e}")

    code = OAuthCallbackHandler.auth_code
    if not code:
        print("\n[!] Local redirect not caught automatically.")
        manual_input = input("Paste the callback URL or authorization code here (or press Enter to cancel): ").strip()
        if not manual_input:
            return False
        if "code=" in manual_input:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(manual_input).query)
            code = parsed.get("code", [manual_input])[0]
        else:
            code = manual_input

    print("[*] Exchanging authorization code for OAuth tokens...")
    try:
        resp = httpx.post(
            TOKEN_URI,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            print(f"[-] Failed exchanging code: {resp.status_code} {resp.text}")
            return False
        tok_data = resp.json()
        refresh_tok = tok_data.get("refresh_token")
        access_tok = tok_data.get("access_token")
        save_tokens(account, client_id, client_secret, refresh_tok or "", access_tok)
        if refresh_tok:
            verify_token(refresh_tok, client_id, client_secret)
        return True
    except Exception as e:
        print(f"[-] Error exchanging code: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Google Drive OAuth Setup for Survey Pipeline")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="Google account email (default: admin@trustbridgevn.com)")
    parser.add_argument("--gcloud", action="store_true", help="Authorize using gcloud CLI login")
    parser.add_argument("--extract-gcloud", action="store_true", help="Extract tokens from existing gcloud database")
    parser.add_argument("--check", action="store_true", help="Verify current Google Drive credentials")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port for local OAuth redirect server")
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_DRIVE_CLIENT_ID", ""), help="Google OAuth Client ID")
    parser.add_argument("--client-secret", default=os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", ""), help="Google OAuth Client Secret")
    parser.add_argument("--refresh-token", default=None, help="Set refresh token directly")
    args = parser.parse_args()

    # 1. Verification check
    if args.check:
        print("[*] Checking configured Google Drive credentials...")
        from services.google_drive_delivery import get_google_oauth_credentials, get_google_service_account_info
        oauth = get_google_oauth_credentials()
        sa = get_google_service_account_info()
        if oauth and oauth.get("refresh_token"):
            print(f"[+] Found OAuth2 refresh token for account: {oauth.get('account', args.account)}")
            ok = verify_token(oauth["refresh_token"], oauth.get("client_id", ""), oauth.get("client_secret", ""))
            return 0 if ok else 1
        elif sa:
            print(f"[+] Found Service Account credentials: {sa.get('client_email')}")
            return 0
        else:
            print("[-] No valid Google Drive credentials found in .env or token.json.")
            return 1

    # 2. Direct refresh token passed
    if args.refresh_token:
        save_tokens(args.account, args.client_id, args.client_secret, args.refresh_token)
        if args.client_id and args.client_secret:
            verify_token(args.refresh_token, args.client_id, args.client_secret)
        return 0

    # 3. Extract from existing gcloud database if requested
    if args.extract_gcloud:
        creds = extract_gcloud_credentials(args.account)
        if creds and creds.get("refresh_token"):
            save_tokens(creds["account"], creds.get("client_id", ""), creds.get("client_secret", ""), creds["refresh_token"])
            verify_token(creds["refresh_token"], creds.get("client_id", ""), creds.get("client_secret", ""))
            return 0
        else:
            print(f"[-] No credentials found in gcloud database for {args.account}")
            return 1

    # 4. Explicit gcloud flow
    if args.gcloud:
        ok = run_gcloud_flow(args.account)
        return 0 if ok else 1

    # 5. Interactive menu if run without explicit flags
    gcloud_path = find_gcloud()
    print("=" * 60)
    print("  TrustBridgeVN Survey Pipeline - Google Drive OAuth Setup")
    print(f"  Target Account: {args.account}")
    print("=" * 60)

    # Check if gcloud already has credentials for account
    existing_gcloud = extract_gcloud_credentials(args.account)
    if existing_gcloud and existing_gcloud.get("refresh_token"):
        print(f"\n[+] Found existing credentials for {args.account} in local gcloud!")
        use_it = input("Would you like to import them into .env and token.json now? [Y/n]: ").strip().lower()
        if use_it in ("", "y", "yes"):
            save_tokens(
                account=existing_gcloud["account"],
                client_id=existing_gcloud.get("client_id", ""),
                client_secret=existing_gcloud.get("client_secret", ""),
                refresh_token=existing_gcloud["refresh_token"],
            )
            verify_token(existing_gcloud["refresh_token"], existing_gcloud.get("client_id", ""), existing_gcloud.get("client_secret", ""))
            return 0

    print("\nPlease choose an authorization method:")
    if gcloud_path:
        print(f"  1. Google Cloud SDK CLI (Detected at {gcloud_path}) [Recommended]")
    else:
        print("  1. Google Cloud SDK CLI (gcloud)")
    print("  2. Desktop / Browser Flow (Custom Client ID & Secret)")
    print("  3. Manually paste Refresh Token")
    print("  4. Exit")

    choice = input("\nEnter choice [1-4] (default 1): ").strip() or "1"

    if choice == "1":
        if not gcloud_path:
            print("[-] gcloud CLI not found. Please select option 2 or install Google Cloud SDK.")
            return 1
        ok = run_gcloud_flow(args.account)
        return 0 if ok else 1

    elif choice == "2":
        cid = args.client_id or input("Enter Google Client ID: ").strip()
        csec = args.client_secret or input("Enter Google Client Secret: ").strip()
        if not cid or not csec:
            print("[-] Client ID and Secret are required for desktop flow.")
            return 1
        ok = run_desktop_flow(cid, csec, args.account, args.port)
        return 0 if ok else 1

    elif choice == "3":
        cid = args.client_id or input("Enter Google Client ID (or leave blank if using gcloud client): ").strip()
        csec = args.client_secret or input("Enter Google Client Secret (or leave blank if using gcloud client): ").strip()
        rf = input("Enter Refresh Token: ").strip()
        if not rf:
            print("[-] Refresh token cannot be empty.")
            return 1
        save_tokens(args.account, cid, csec, rf)
        if cid and csec:
            verify_token(rf, cid, csec)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
