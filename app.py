from fastapi import FastAPI, Request
from datetime import datetime, timedelta
import os
import json
import pickle

# Google libraries
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = FastAPI()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# In-memory token store (for testing)
user_tokens = {}  # {student_username: credentials pickle}


# ------------------------
# OAuth endpoints
# ------------------------
@app.get("/auth/login")
def login(student: str):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )
    flow.params["login_hint"] = student
    auth_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true"
    )
    return {"url": auth_url}


@app.get("/auth/callback")
def auth_callback(code: str, student: str):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    # Save credentials (in-memory for now)
    user_tokens[student] = creds
    return {"status": "success"}


# ------------------------
# GitHub webhook
# ------------------------
@app.post("/webhook")
async def github_webhook(request: Request):
    payload = await request.json()
    event = request.headers.get("X-GitHub-Event")

    if event == "repository" and payload.get("action") == "created":
        repo_name = payload["repository"]["name"]
        owner = payload["repository"]["owner"]["login"]

        accepted_at = payload["repository"]["created_at"]
        received_at = datetime.utcnow().isoformat() + "Z"

        print("✅ Assignment accepted")
        print("Repo:", repo_name)
        print("GitHub username:", owner)
        print("GitHub timestamp:", accepted_at)
        print("Webhook received at:", received_at)

        # ----- Google Calendar Integration -----
        # Look up student credentials
        creds = user_tokens.get(owner)
        if creds:
            service = build("calendar", "v3", credentials=creds)
            # Example: create event due 1 week from repo creation
            due_date = datetime.fromisoformat(
                accepted_at.replace("Z", "+00:00")
            ) + timedelta(days=7)
            event = {
                "summary": f"{repo_name} due",
                "description": f"Assignment {repo_name} accepted",
                "start": {"dateTime": accepted_at, "timeZone": "UTC"},
                "end": {"dateTime": due_date.isoformat(), "timeZone": "UTC"},
            }
            created = service.events().insert(calendarId="primary", body=event).execute()
            print("📅 Google Calendar event created:", created["id"])
        else:
            print(f"⚠️ No Google credentials for {owner}. Student must log in.")

    return {"status": "ok"}
