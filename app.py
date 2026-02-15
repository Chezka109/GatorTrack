import os
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = FastAPI()

# =============================
# Environment Variables
# =============================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Temporary in-memory token store (fine for prototype)
user_tokens = {}


# =============================
# Health & Root
# =============================
@app.get("/")
def root():
    return {"status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


# =============================
# Google OAuth Login
# =============================
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

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        login_hint=student,
        prompt="consent",
    )

    return {"auth_url": authorization_url}


# =============================
# OAuth Callback
# =============================
@app.get("/auth/callback")
def callback(code: str, state: str = None):
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

    credentials = flow.credentials
    user_tokens["student"] = credentials

    return {"status": "Google Calendar connected successfully"}


# =============================
# GitHub Webhook
# =============================
@app.post("/webhook")
async def github_webhook(request: Request):
    payload = await request.json()

    assignment_name = payload.get("repository", {}).get("name", "Unknown Assignment")

    creds = user_tokens.get("student")

    if creds:
        service = build("calendar", "v3", credentials=creds)

        event = {
            "summary": f"{assignment_name} Due",
            "description": "GitHub Classroom assignment",
            "start": {
                "dateTime": "2026-02-20T23:59:00",
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": "2026-02-21T00:00:00",
                "timeZone": "UTC",
            },
        }

        service.events().insert(calendarId="primary", body=event).execute()

    return {"status": "webhook received"}
