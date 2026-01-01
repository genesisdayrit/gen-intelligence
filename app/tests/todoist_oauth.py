#!/usr/bin/env python3
"""Todoist OAuth helper - get an access token for your account.

Usage:
    python tests/todoist_oauth.py

This script will:
1. Open your browser to authorize your Todoist app
2. Start a local server to receive the callback
3. Exchange the authorization code for an access token
4. Print the access token for you to add to .env
"""

import os
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import secrets

import httpx
from dotenv import load_dotenv

load_dotenv()

TODOIST_CLIENT_ID = os.environ.get("TODOIST_CLIENT_ID")
TODOIST_CLIENT_SECRET = os.environ.get("TODOIST_CLIENT_SECRET")

REDIRECT_PORT = 8888
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

# Store the authorization code when received
auth_code = None
state_token = secrets.token_urlsafe(16)


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback from Todoist."""

    def do_GET(self):
        global auth_code

        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)

            # Verify state
            received_state = params.get("state", [None])[0]
            if received_state != state_token:
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Error: State mismatch</h1>")
                return

            # Get code
            auth_code = params.get("code", [None])[0]
            if auth_code:
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h1>Authorization successful!</h1>"
                    b"<p>You can close this window and return to the terminal.</p>"
                )
            else:
                error = params.get("error", ["Unknown error"])[0]
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h1>Error: {error}</h1>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logging


def main():
    if not TODOIST_CLIENT_ID:
        print("Error: TODOIST_CLIENT_ID not set in .env")
        print("\n1. Go to https://developer.todoist.com/appconsole.html")
        print("2. Create an app and get your Client ID")
        print("3. Add TODOIST_CLIENT_ID to your .env file")
        sys.exit(1)

    if not TODOIST_CLIENT_SECRET:
        print("Error: TODOIST_CLIENT_SECRET not set in .env")
        print("Add TODOIST_CLIENT_SECRET to your .env file")
        sys.exit(1)

    print("Todoist OAuth Flow")
    print("=" * 40)
    print(f"\nIMPORTANT: First, add this redirect URI to your Todoist app settings:")
    print(f"  {REDIRECT_URI}")
    print("\nGo to: https://developer.todoist.com/appconsole.html")
    print("Edit your app -> OAuth Redirect URL -> Add the URL above")
    input("\nPress Enter when ready...")

    # Build authorization URL
    auth_url = (
        f"https://todoist.com/oauth/authorize"
        f"?client_id={TODOIST_CLIENT_ID}"
        f"&scope=data:read_write"
        f"&state={state_token}"
    )

    print(f"\nOpening browser for authorization...")
    print(f"If browser doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Start local server to receive callback
    print(f"Waiting for callback on http://localhost:{REDIRECT_PORT}...")
    server = HTTPServer(("localhost", REDIRECT_PORT), OAuthCallbackHandler)

    # Handle one request
    server.handle_request()

    if not auth_code:
        print("\nError: No authorization code received")
        sys.exit(1)

    print(f"\nReceived authorization code!")
    print("Exchanging for access token...")

    # Exchange code for token
    response = httpx.post(
        "https://todoist.com/oauth/access_token",
        data={
            "client_id": TODOIST_CLIENT_ID,
            "client_secret": TODOIST_CLIENT_SECRET,
            "code": auth_code,
        },
    )

    if response.status_code == 200:
        result = response.json()
        access_token = result.get("access_token")
        print("\n" + "=" * 40)
        print("SUCCESS! Add this to your .env file:")
        print("=" * 40)
        print(f"\nTODOIST_ACCESS_TOKEN={access_token}")
        print("\n" + "=" * 40)
    else:
        print(f"\nError exchanging code for token:")
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
