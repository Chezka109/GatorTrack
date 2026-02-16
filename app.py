import os
import json
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = FastAPI()

# ==============================
# ENV VARIABLES (Render)
# ==============================

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Temporary in-memory storage (fine for MVP)
user_tokens = {}

# ==============================
# HEALTH CHECK
# ==============================


@app.get("/health")
def health():
    return {"status": "ok"}


# ==============================
# GOOGLE LOGIN
# ==============================


@app.get("/auth/login")
def login():
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

    auth_url, _ = flow.authorization_url(prompt="consent")

    return RedirectResponse(auth_url)


# ==============================
# GOOGLE CALLBACK
# ==============================


@app.get("/auth/callback")
async def callback(request: Request):
    error = request.query_params.get("error")
    code = request.query_params.get("code")

    if error:
        return JSONResponse({"error": error}, status_code=400)

    if not code:
        return JSONResponse({"error": "No code returned"}, status_code=400)

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

        # Store credentials (MVP only)
        user_tokens["student"] = creds

        return {"status": "Google Calendar connected"}

    except Exception as e:
        print("OAuth error:", e)
        return JSONResponse({"error": str(e)}, status_code=400)


# ==============================
# SAFE DATE PARSER
# ==============================


def parse_due_date(raw_due):
    if not raw_due:
        now = datetime.now()
        return now.replace(hour=23, minute=59, second=0, microsecond=0)

    if isinstance(raw_due, str):
        if len(raw_due) == 10:
            raw_due += "T23:59:00"
        return datetime.fromisoformat(raw_due)

    if isinstance(raw_due, datetime):
        return raw_due

    raise ValueError("Invalid due date format")


# ==============================
# CREATE GOOGLE EVENT
# ==============================


def create_calendar_event(creds, title, description, due_date):
    service = build("calendar", "v3", credentials=creds)

    due_datetime = parse_due_date(due_date)
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


# ==============================
# GITHUB WEBHOOK
# ==============================


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    print("Webhook received:", data)

    creds = user_tokens.get("student")
    if not creds:
        return {"error": "User not authenticated with Google"}

    try:
        # Try extracting assignment info safely
        title = None
        due_date = None

        # GitHub Classroom repo creation
        if "repository" in data:
            title = data["repository"]["name"]

        # If your payload includes deadline somewhere
        if "assignment" in data:
            due_date = data["assignment"].get("deadline")

        if not title:
            title = "New Assignment"

        event_link = create_calendar_event(
            creds,
            title=title,
            description="GitHub Classroom assignment",
            due_date=due_date,
        )

        return {"status": "Assignment added to calendar", "event_link": event_link}

    except Exception as e:
        print("Webhook error:", e)
        return JSONResponse({"error": str(e)}, status_code=400)
