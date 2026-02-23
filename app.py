import os
import requests
from time import time
from datetime import datetime, timedelta
import pytz

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

EASTERN_TZ = pytz.timezone("America/New_York")

# ==============================
# ENV VARIABLES
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

user_tokens = {}  # single student for MVP
assignment_cache = {"data": None, "timestamp": 0}
event_mapping = {}  # slug -> event_id


# ==============================
# HEALTH CHECK
# ==============================


@app.get("/health")
def health():
    return {"status": "ok"}


# ==============================
# GOOGLE OAUTH
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


@app.get("/auth/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        return JSONResponse({"error": error}, status_code=400)

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


# ==============================
# GITHUB CLASSROOM
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


# ==============================
# GOOGLE CALENDAR EVENT
# ==============================


def create_or_update_event(creds, slug, title, description, deadline_iso):
    # Refresh token if needed
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())

    service = build("calendar", "v3", credentials=creds)  # type: ignore

    # -----------------------
    # Deadline Handling
    # -----------------------

    if deadline_iso:
        utc_dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone(EASTERN_TZ)

        start = {
            "dateTime": local_dt.isoformat(),
            "timeZone": "America/New_York",
        }

        end = {
            "dateTime": local_dt.isoformat(),  # deadline = deadline
            "timeZone": "America/New_York",
        }

    else:
        # All-day event (must end next day)
        today = datetime.now(EASTERN_TZ).date()
        next_day = today + timedelta(days=1)

        start = {"date": today.isoformat()}
        end = {"date": next_day.isoformat()}

    event_body = {
        "summary": title,
        "description": description,
        "start": start,
        "end": end,
    }

    # -----------------------
    # Update or Create
    # -----------------------

    if slug in event_mapping:
        event_id = event_mapping[slug]

        updated = (
            service.events()
            .update(calendarId="primary", eventId=event_id, body=event_body)
            .execute()
        )

        return updated.get("htmlLink")

    else:
        created = service.events().insert(calendarId="primary", body=event_body).execute()

        event_mapping[slug] = created["id"]
        return created.get("htmlLink")


# ==============================
# WEBHOOK (CREATE EVENT)
# ==============================


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    creds = user_tokens.get("student")
    if not creds:
        return {"error": "User not authenticated with Google"}

    if "repository" not in data:
        return {"message": "Not a repository event"}

    repo_name = data["repository"]["name"].lower()
    print("Repo name:", repo_name)

    assignments = get_classroom_assignments()

    assignment = None

    for a in assignments:
        slug = a["slug"].lower()
        print("Checking slug:", slug)

        if repo_name.startswith(slug):
            assignment = a
            break

    if not assignment:
        print("No assignment matched")
        return {"error": "Assignment not found"}

    deadline = assignment.get("deadline")

    event_link = create_or_update_event(
        creds,
        slug=assignment["slug"],
        title=assignment["title"],
        description="GitHub Classroom assignment",
        deadline_iso=deadline,
    )

    return {"status": "Event created/updated", "event_link": event_link}


# ==============================
# BACKGROUND SYNC (UPDATE ONLY)
# ==============================


def sync_assignments():
    creds = user_tokens.get("student")
    if not creds:
        return

    assignments = get_classroom_assignments()

    for assignment in assignments:
        slug = assignment["slug"]

        # ONLY update events that already exist
        if slug not in event_mapping:
            continue

        deadline = assignment.get("deadline")

        create_or_update_event(
            creds,
            slug=slug,
            title=assignment["title"],
            description="GitHub Classroom assignment (auto-sync)",
            deadline_iso=deadline,
        )

    print(f"[{datetime.now(EASTERN_TZ)}] Sync complete")


scheduler.add_job(sync_assignments, "interval", minutes=10)


# ==============================
# DEBUG
# ==============================


@app.get("/debug/assignments")
def debug_assignments():
    return get_classroom_assignments()


@app.get("/debug/events")
def debug_events():
    return event_mapping
