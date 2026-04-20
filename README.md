# GatorTrack

GatorTrack is a research prototype that syncs GitHub Classroom assignment deadlines into a student’s Google Calendar.

## Research problem

Students often miss deadlines because assignments live in tools they don’t check continuously (GitHub Classroom/LMS), while daily planning happens in a calendar. The research question behind GatorTrack is:

**Can we reduce missed deadlines and planning overhead by automatically projecting assignment deadlines into the calendar the moment a student accepts an assignment (and keeping those events up to date)?**

This repo implements a feasible MVP and includes evaluation harnesses (load + failure-injection) to quantify reliability.

## What it does (end-to-end)

1. **Student connects Google Calendar**
	 - Student visits `/connect`, enters their GitHub username, and completes Google OAuth.

2. **GitHub Classroom acceptance triggers webhook**
	 - A webhook hits `POST /webhook` with repository info when an assignment is accepted.

3. **Fetch assignment metadata**
	 - The server queries the GitHub Classroom API for assignments (with caching).

4. **Create/update a Google Calendar event**
	 - Creates a calendar event for the assignment deadline.
	 - Updates the existing event when the deadline changes (avoids duplicates via mapping).

## Prototype constraints (important)

This is an MVP/research prototype:

- **In-memory storage**: OAuth tokens and mappings are stored in process memory (restart clears state).
- **Not production-hardened**: no database, no multi-instance coordination, no user auth beyond “GitHub username entered + OAuth”.
- **Safe failure goal**: malformed webhook payloads should never crash the server; they should return clear errors.

## Installation (local)

### Prerequisites

- Python 3.11+ (works in the provided venv)
- A Google Cloud OAuth Client (Web application)
- A GitHub token with access to the classroom/org you’re testing against
- GitHub Classroom `CLASSROOM_ID`

### 1) Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

### 3) Configure environment variables

Export these variables in your shell/session (or set them in your deployment environment):

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export GOOGLE_REDIRECT_URI="http://localhost:8000/auth/callback"

export GITHUB_TOKEN="..."
export CLASSROOM_ID="..."  # numeric string
```

Notes:

- `GOOGLE_REDIRECT_URI` must be registered in your Google OAuth client.
- `GITHUB_TOKEN` is sent as a Bearer token to the GitHub API.

### 4) Run the server

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Verify health:

```bash
curl http://localhost:8000/health
```

## Deployment (Render)

This prototype is designed to run as a small FastAPI service on Render.

1. Create a new **Web Service** on Render and connect this Git repo.
2. Set the **Start Command** (or use the included Procfile):

	```
	uvicorn app:app --host 0.0.0.0 --port $PORT
	```

3. Add the required environment variables in Render:

	- `GOOGLE_CLIENT_ID`
	- `GOOGLE_CLIENT_SECRET`
	- `GOOGLE_REDIRECT_URI` (must match your Render URL, e.g. `https://YOUR.onrender.com/auth/callback`)
	- `GITHUB_TOKEN`
	- `CLASSROOM_ID`

4. Update your Google OAuth client’s **Authorized redirect URIs** to include the Render callback URL.

Once deployed, use the public base URL for the evaluation scripts (load/failure tests).

## Usage (for someone other than the creator)

### A) Connect a student’s Google Calendar

1. Open `http://localhost:8000/connect`
2. Enter **your GitHub username** (this is used to match webhook repo names)
3. Complete Google OAuth consent

Expected result: the callback returns a JSON status like `"<username> connected successfully"`.

### B) Configure a webhook (real integration)

Point the GitHub (org/classroom) webhook to:

- `POST https://<your-host>/webhook`

The handler expects a JSON body that includes repository info, e.g.:

```json
{
	"repository": {
		"name": "lab-1-yourGithubUsername",
		"owner": {"login": "classroom-org"}
	}
}
```

The server extracts the GitHub username from the **last dash-separated segment** of the repo name.

### C) Demo without a real webhook (manual simulation)

After connecting via `/connect`, simulate an acceptance event:

```bash
curl -X POST http://localhost:8000/webhook \
	-H 'Content-Type: application/json' \
	-d '{"repository":{"name":"lab-1-yourGithubUsername","owner":{"login":"classroom-org"}}}'
```

Expected result: a JSON response indicating an event was created/updated (or a safe error if the assignment can’t be found).

## Debug & monitoring endpoints

These are intentionally lightweight for the MVP:

- `GET /debug/assignments` — fetch cached classroom assignments
- `GET /debug/connected-users` — users connected in this process
- `GET /debug/event-log` — last ~50 event create/update actions
- `GET /debug/event-mappings` — mapping of (user, assignment) → event_id
- `POST /debug/force-sync` — run a sync immediately
- `POST /debug/clear-cache` — force fresh GitHub API fetch next time

## Error handling & validation (what’s implemented)

The API is designed to fail safely:

- Request timeouts are used for GitHub API calls.
- Webhook payload validation includes:
	- non-repository payloads are ignored (`{"message": "Not a repository event"}`)
	- invalid/missing `repository` or `repository.name` returns a **400** with a clear error
	- users who never connected return `{"status": "user_not_connected"}` (no crash)
- Exceptions from GitHub requests and Google Calendar API are caught and returned as structured JSON errors.

## Experimental evaluation (supports the report)

This repo includes two complementary evaluations:

### 1) Realistic load / availability run

Goal: measure uptime-like availability and latency under expected classroom-like use.

Run:

```bash
python -m evaluation.load_test \
	--base-url https://YOUR.onrender.com \
	--users 30 \
	--webhooks-per-hour 100 \
	--duration-seconds 1800
```

Outputs:

- JSONL request log in `evaluation_logs/`
- Summary JSON (`*_summary.json`) with:
	- availability %
	- latency percentiles (p50/p90/p95/p99)
	- MTBF derived from failure timestamps

### 2) Failure-injection run

Goal: ensure malformed input and missing state are handled predictably.

Run:

```bash
python -m evaluation.failure_tests --base-url https://YOUR.onrender.com
```

Measures:

- pass rate across failure cases
- observed status codes
- latency of failure handling

### Generate charts for your slides

Charts are generated from the saved summary JSON files:

```bash
python -m evaluation.make_charts \
	--load-summary evaluation_logs/load_20260401T150749Z_summary.json \
	--failure-summary evaluation_logs/failure_20260401T020656Z_summary.json \
	--out-dir evaluation_charts
```

This creates PNGs in `evaluation_charts/` (ready to paste into Google Slides).

### Example results snapshot (from the report run)

Realistic Load Test (30 users, 100 webhooks/hr, ~30 minutes):

- Availability (overall): **96.58%**
- Latency (ms): p50 **72.4**, p90 **89.1**, p95 **119.4**, p99 **10087.3** (rare tail events)

Failure-Injection (5 cases):

- Pass rate: **80%**
- Status codes: **200 × 4**, **500 × 1**

## Software development best practices (what’s present, and what to improve)

Implemented practices in this prototype:

- Clear separation between app runtime (`app.py`) and experiments (`evaluation/`)
- Timeouts on external HTTP calls
- Input validation on webhook payloads + safe error responses
- Caching to reduce GitHub API calls (`assignment_cache`)
- Reproducible experiment harnesses that emit machine-readable logs (JSONL/JSON)

Planned upgrades to improve engineering rigor:

- Persistent storage (DB) for tokens and mappings
- Structured logging + request IDs
- Unit tests + CI to prevent regression (especially around webhook validation)
- Production auth model (don’t trust a raw username form field)

## Evidence of regular commits

This repo includes a commit history you can inspect with:

```bash
git log --oneline --decorate --graph
```

Current history summary (from `git log`):

- **41 commits**
- Development window recorded in git: **2026-02-02 → 2026-02-23**
- Commits appear across multiple days (e.g., 2026-02-02, 02-04, 02-08, 02-15, 02-18, 02-22, 02-23)

## Technical complexity (junior-level appropriate)

GatorTrack demonstrates junior-level full-stack backend complexity:

- FastAPI web service with HTML + JSON endpoints
- OAuth flow and integration with Google Calendar API
- GitHub API integration (Classroom assignments)
- Background scheduling for periodic sync
- Quantitative evaluation harnesses (load + failure injection) and chart generation

## Conclusions (from the research prototype)

- Overall, the research prototype effectively balances feasibility and technical challenge.
- Overall, the research prototype shows a promising foundation for senior-level research.
