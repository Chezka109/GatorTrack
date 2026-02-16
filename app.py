import os
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from datetime import datetime, timedelta

app = FastAPI()


def create_calendar_event(creds, title, description, due_date_iso):
    service = build("calendar", "v3", credentials=creds)

    # Parse due date safely
    due_datetime = datetime.fromisoformat(due_date_iso)

    # Make event 1 hour long
    end_datetime = due_datetime + timedelta(hours=1)

    event = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": due_datetime.isoformat(),
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": end_datetime.isoformat(),
            "timeZone": "America/New_York",
        },
    }

    created_event = service.events().insert(calendarId="primary", body=event).execute()

    return created_event.get("htmlLink")


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
async def callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        print("Google returned error:", error)
        return {"error": error}

    if not code:
        return {"error": "No code in query params"}

    try:
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
        user_tokens["student"] = creds
        return {"status": "Google Calendar connected"}
    except Exception as e:
        print("OAuth Exception:", e)
        return {"error": str(e)}


# =============================
# GitHub Webhook
# =============================
@app.post("/webhook")
async def webhook(data: dict):
    title = data.get("title")
    description = data.get("description", "")
    due_date = data.get("due_date")  # ISO format

    creds = user_tokens.get("student")

    if not creds:
        return {"error": "User not authenticated"}

    link = create_calendar_event(creds, title, description, due_date)

    return {"status": "Assignment added to calendar", "event_link": link}
