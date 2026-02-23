import os
import json
import requests
from time import time
from datetime import datetime
import pytz

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
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
user_tokens = {}  # github_username -> Google credentials
assignment_cache = {"data": None, "timestamp": 0}
event_mapping = {}  # (github_username, assignment_slug) -> event_id


# ==============================
# HEALTH CHECK
# ==============================
@app.get("/health")
def health():
    return {"status": "ok"}


# ==============================
# CENTRALIZED CONNECT PAGE
# ==============================
@app.get("/connect", response_class=HTMLResponse)
def connect_page():
    return """
    <html>
        <body>
            <h2>Connect Your Google Calendar</h2>
            <form action="/start-auth" method="post">
                <label>GitHub Username:</label>
                <input type="text" name="github_username" required />
                <button type="submit">Connect</button>
            </form>
        </body>
    </html>
    """


# ==============================
# START GOOGLE AUTH
# ==============================
@app.post("/start-auth")
def start_auth(github_username: str = Form(...)):
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

    authorization_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=github_username,  # store username in OAuth state
    )

    return RedirectResponse(authorization_url)


# ==============================
# GOOGLE CALLBACK
# ==============================
@app.get("/auth/callback")
async def callback(request: Request):
    error = request.query_params.get("error")
    if error:
        return JSONResponse({"error": error}, status_code=400)

    github_username = request.query_params.get("state")
    code = request.query_params.get("code")

    if not code or not github_username:
        return JSONResponse({"error": "Invalid OAuth callback"}, status_code=400)

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

    user_tokens[github_username] = creds

    return {"status": f"{github_username} connected successfully"}


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
# CREATE OR UPDATE EVENT
# ==============================
def create_or_update_event(
    creds, github_username, assignment_slug, title, description, deadline_iso
):
    service = build("calendar", "v3", credentials=creds)

    if deadline_iso:
        if "T" in deadline_iso:
            utc_dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(EASTERN_TZ)
            start = {"dateTime": local_dt.isoformat(), "timeZone": "America/New_York"}
            end = {"dateTime": local_dt.isoformat(), "timeZone": "America/New_York"}
        else:
            start = {"date": deadline_iso}
            end = {"date": deadline_iso}
    else:
        today = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
        start = {"date": today}
        end = {"date": today}

    event_body = {
        "summary": title,
        "description": description,
        "start": start,
        "end": end,
    }

    key = (github_username, assignment_slug)

    if key in event_mapping:
        event_id = event_mapping[key]
        updated = (
            service.events()
            .update(calendarId="primary", eventId=event_id, body=event_body)
            .execute()
        )
        return updated.get("htmlLink")
    else:
        created = service.events().insert(calendarId="primary", body=event_body).execute()
        event_mapping[key] = created["id"]
        return created.get("htmlLink")


# ==============================
# GITHUB WEBHOOK
# ==============================
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Webhook payload:", data)

    if "repository" not in data:
        return {"message": "Not a repository event"}

    github_username = data["repository"]["owner"]["login"]
    print("Stored users:", user_tokens.keys())
    print("Incoming username:", github_username)

    repo_name = data["repository"]["name"].lower()

    creds = user_tokens.get(github_username)
    if not creds:
        return {"status": "user_not_connected"}

    try:
        assignments = get_classroom_assignments()
        assignment = find_assignment_by_repo(repo_name, assignments)

        if not assignment:
            return {"error": "Assignment not found"}

        if assignment.get("accepted", 0) < 1:
            return {"message": "Assignment not accepted, skipping"}

        deadline = assignment.get("deadline")

        event_link = create_or_update_event(
            creds,
            github_username=github_username,
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
# AUTO SYNC
# ==============================
def sync_assignments():
    try:
        assignments = get_classroom_assignments()

        for github_username, creds in user_tokens.items():
            for assignment in assignments:
                if assignment.get("accepted", 0) < 1:
                    continue

                slug = assignment["title"].lower().replace(" ", "-")
                deadline = assignment.get("deadline")

                create_or_update_event(
                    creds,
                    github_username=github_username,
                    assignment_slug=slug,
                    title=assignment["title"],
                    description="GitHub Classroom assignment (auto-sync)",
                    deadline_iso=deadline,
                )

        print(f"[{datetime.now(EASTERN_TZ)}] Auto-sync completed")

    except Exception as e:
        print("Auto-sync error:", e)


scheduler.add_job(sync_assignments, "interval", minutes=10)


# ==============================
# DEBUG
# ==============================
@app.get("/debug/assignments")
def debug_assignments():
    try:
        return get_classroom_assignments()
    except Exception as e:
        return {"error": str(e)}
