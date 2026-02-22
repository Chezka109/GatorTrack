import os
import json
import requests
from time import time
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

EASTERN_TZ = pytz.timezone("America/New_York")

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

# ==============================
# TEMP STORAGE (MVP)
# ==============================

user_tokens = {}  # student_id -> Google credentials
assignment_cache = {"data": None, "timestamp": 0}  # assignments cache
event_mapping = {}  # assignment_slug -> {"event_id": str, "student": str}

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
        user_tokens["student"] = creds  # for MVP, single student

        return {"status": "Google Calendar connected"}
    except Exception as e:
        print("OAuth error:", e)
        return JSONResponse({"error": str(e)}, status_code=400)


# ==============================
# GITHUB CLASSROOM API
# ==============================


def get_classroom_assignments():
    global assignment_cache
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
# GOOGLE CALENDAR EVENT CREATION/UPDATE
# ==============================


def create_or_update_event(creds, assignment_slug, title, description, deadline_iso):
    service = build("calendar", "v3", credentials=creds)
    # -----------------------
    # Parse Deadline
    # -----------------------
    if deadline_iso:
        if "T" in deadline_iso:
            utc_dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(EASTERN_TZ)
            end_dt = local_dt + timedelta(hours=1)
            start = {"dateTime": local_dt.isoformat(), "timeZone": "America/New_York"}
            end = {"dateTime": end_dt.isoformat(), "timeZone": "America/New_York"}
        else:
            start = {"date": deadline_iso}
            end = {"date": deadline_iso}
    else:
        now = datetime.now(EASTERN_TZ).replace(
            hour=23, minute=59, second=0, microsecond=0
        )
        end_dt = now + timedelta(hours=1)
        start = {"dateTime": now.isoformat(), "timeZone": "America/New_York"}
        end = {"dateTime": end_dt.isoformat(), "timeZone": "America/New_York"}

    event_body = {
        "summary": title,
        "description": description,
        "start": start,
        "end": end,
    }

    # -----------------------
    # Update if exists
    # -----------------------
    if assignment_slug in event_mapping:
        event_id = event_mapping[assignment_slug]["event_id"]
        updated_event = (
            service.events()
            .update(calendarId="primary", eventId=event_id, body=event_body)
            .execute()
        )
        return updated_event.get("htmlLink")
    else:
        created_event = (
            service.events().insert(calendarId="primary", body=event_body).execute()
        )
        event_mapping[assignment_slug] = {
            "event_id": created_event["id"],
            "student": "student",
        }
        return created_event.get("htmlLink")


# ==============================
# GITHUB WEBHOOK
# ==============================


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Webhook payload:", data)

    creds = user_tokens.get("student")
    if not creds:
        return {"error": "User not authenticated with Google"}

    try:
        if "repository" not in data:
            return {"message": "Not a repository event"}

        repo_name = data["repository"]["name"].lower()
        print("Attempting to match repo:", repo_name)
        assignments = get_classroom_assignments()

        assignment = None
        for a in assignments:
            slug = a["title"].lower().replace(" ", "-")
            print("Checking assignment slug:", slug)
            if repo_name.startswith(slug):
                print("Matched assignment:", a["title"])
                assignment = a
                break
        if not assignment:
            print("No assignment matched")
            return {"error": "Assignment not found"}

        deadline = assignment.get("deadline")
        event_link = create_or_update_event(
            creds,
            assignment_slug=assignment["title"].lower().replace(" ", "-"),
            title=assignment["title"],
            description="GitHub Classroom assignment",
            deadline_iso=deadline,
        )

        return {"status": "Assignment added/updated", "event_link": event_link}

    except Exception as e:
        print("Webhook error:", e)
        return JSONResponse({"error": str(e)}, status_code=400)


# ==============================
# BACKGROUND TASK: AUTOMATIC SYNC
# ==============================


def sync_assignments():
    creds = user_tokens.get("student")
    if not creds:
        print("Sync skipped: student not authenticated")
        return

    try:
        assignments = get_classroom_assignments()
        for assignment in assignments:
            slug = assignment["title"].lower().replace(" ", "-")
            deadline = assignment.get("deadline")
            create_or_update_event(
                creds,
                assignment_slug=slug,
                title=assignment["title"],
                description="GitHub Classroom assignment (auto-sync)",
                deadline_iso=deadline,
            )
        print(f"[{datetime.now(EASTERN_TZ)}] Auto-sync completed")
    except Exception as e:
        print("Auto-sync error:", e)


# Run every 10 minutes
scheduler.add_job(sync_assignments, "interval", minutes=10)

# ==============================
# DEBUG ENDPOINTS
# ==============================


@app.get("/debug/assignments")
def debug_assignments():
    try:
        return get_classroom_assignments()
    except Exception as e:
        return {"error": str(e)}


@app.get("/debug/test-event")
def test_event():
    creds = user_tokens.get("student")
    if not creds:
        return {"error": "User not authenticated with Google"}

    repo_name = "test-local-19-Chezka109"
    assignments = get_classroom_assignments()
    assignment = find_assignment_by_repo(repo_name, assignments)
    if not assignment:
        return {"error": "Assignment not found"}

    deadline = assignment.get("deadline")
    event_link = create_or_update_event(
        creds,
        assignment_slug=assignment["title"].lower().replace(" ", "-"),
        title=assignment["title"],
        description="GitHub Classroom assignment (TEST)",
        deadline_iso=deadline,
    )

    return {
        "status": "Test event created",
        "event_link": event_link,
        "matched_assignment": assignment["title"],
        "deadline": deadline,
    }
