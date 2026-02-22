import os
import json
import requests
from time import time
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

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
CLASSROOM_ID = os.getenv("CLASSROOM_ID")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# Temporary in-memory storage (fine for MVP)
user_tokens = {}

# Assignment cache (10 minute TTL)
assignment_cache = {"data": None, "timestamp": 0}

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
        user_tokens["student"] = creds

        return {"status": "Google Calendar connected"}

    except Exception as e:
        print("OAuth error:", e)
        return JSONResponse({"error": str(e)}, status_code=400)


# ==============================
# GITHUB CLASSROOM API
# ==============================


def get_classroom_assignments():
    global assignment_cache

    # Return cached if under 10 minutes old
    if assignment_cache["data"] and time() - assignment_cache["timestamp"] < 600:
        return assignment_cache["data"]

    url = f"https://api.github.com/classrooms/{CLASSROOM_ID}/assignments"
    response = requests.get(url, headers=GITHUB_HEADERS)
    response.raise_for_status()

    assignments = response.json()

    assignment_cache["data"] = assignments
    assignment_cache["timestamp"] = time()

    return assignments


def find_assignment_by_repo(repo_name, assignments):
    repo_name = repo_name.lower()

    for assignment in assignments:
        slug = assignment["title"].lower().replace(" ", "-")
        if repo_name.startswith(slug):
            return assignment

    return None


# ==============================
# CREATE GOOGLE EVENT
# ==============================


def create_calendar_event(creds, title, description, deadline_iso):
    service = build("calendar", "v3", credentials=creds)

    if deadline_iso:
        # GitHub returns ISO string like 2026-03-01T23:59:00Z
        start_time = deadline_iso
        end_dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00")) + timedelta(
            hours=1
        )

        event = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start_time,
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "UTC",
            },
        }

    else:
        # Fallback if no deadline
        now = datetime.utcnow()
        end = now + timedelta(hours=1)

        event = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": now.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": "UTC",
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

    print("FULL PAYLOAD:", data)

    creds = user_tokens.get("student")
    if not creds:
        return {"error": "User not authenticated with Google"}

    try:
        if "repository" not in data:
            return {"message": "Not a repository event"}

        repo_name = data["repository"]["name"]

        assignments = get_classroom_assignments()
        assignment = find_assignment_by_repo(repo_name, assignments)

        if not assignment:
            return {"error": "Assignment not found"}

        deadline = assignment.get("deadline")

        event_link = create_calendar_event(
            creds,
            title=assignment["title"],
            description="GitHub Classroom assignment",
            deadline_iso=deadline,
        )

        return {
            "status": "Assignment added to calendar",
            "event_link": event_link,
        }

    except Exception as e:
        print("Webhook error:", e)
        return JSONResponse({"error": str(e)}, status_code=400)


# ==============================
# TEMP DEBUG ENDPOINT
# ==============================


@app.get("/debug/assignments")
def debug_assignments():
    try:
        assignments = get_classroom_assignments()
        return assignments
    except Exception as e:
        return {"error": str(e)}
