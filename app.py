from fastapi import FastAPI, Request, Header, HTTPException
from datetime import datetime
import os
import hmac
import hashlib
import json

app = FastAPI()

# Load webhook secret from environment variables
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """
    Verify GitHub webhook signature (optional but recommended).
    """
    if WEBHOOK_SECRET is None:
        # Allow unsigned webhooks during development
        return True

    if signature is None:
        return False

    try:
        sha_name, signature = signature.split("=")
    except ValueError:
        return False

    if sha_name != "sha256":
        return False

    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)

    return hmac.compare_digest(mac.hexdigest(), signature)


@app.get("/health")
def health_check():
    """
    Simple health check endpoint.
    """
    return {"status": "ok"}


@app.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None),
):
    """
    GitHub webhook endpoint.
    """
    raw_body = await request.body()

    # Verify signature
    if not verify_github_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Log event type
    print("📦 GitHub Event:", x_github_event)

    # Only handle assignment acceptance (repo created)
    if x_github_event == "repository" and payload.get("action") == "created":
        repository = payload.get("repository", {})
        owner = repository.get("owner", {})

        assignment_repo = repository.get("name")
        student_username = owner.get("login")

        # Timestamp when assignment was accepted
        accepted_at = repository.get("created_at")

        # Timestamp when webhook was received
        received_at = datetime.utcnow().isoformat() + "Z"

        print("✅ Assignment accepted")
        print("Student:", student_username)
        print("Repo:", assignment_repo)
        print("GitHub timestamp:", accepted_at)
        print("Webhook received at:", received_at)

        # TODO:
        # 1. Look up assignment due date (GitHub Classroom API)
        # 2. Create Google Calendar event
        # 3. Store event ID to prevent duplicates

    else:
        # Ignore all other events
        print("ℹ️ Event ignored")

    return {"status": "ok"}
