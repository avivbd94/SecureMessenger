"""
Webhook server – receives Fireflies events and immediately syncs to HubSpot.

Run:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000

Configure in Fireflies:
    Settings → Integrations → Webhooks → https://your-domain/webhook/fireflies

Supported events:
    Transcription completed  →  create call in HubSpot
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

load_dotenv()

FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
WEBHOOK_SECRET = os.getenv("FIREFLIES_WEBHOOK_SECRET", "")  # optional HMAC verification

FIREFLIES_URL = "https://api.fireflies.ai/graphql"
HUBSPOT_BASE = "https://api.hubapi.com"

app = FastAPI(title="Fireflies → HubSpot webhook bridge")


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def verify_signature(payload: bytes, signature: str | None) -> None:
    """Verify Fireflies HMAC-SHA256 signature if a secret is configured."""
    if not WEBHOOK_SECRET:
        return
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")
    expected = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")


# ---------------------------------------------------------------------------
# Fireflies helpers
# ---------------------------------------------------------------------------

def fetch_transcript(transcript_id: str) -> dict:
    query = """
    query Transcript($id: String!) {
      transcript(id: $id) {
        id
        title
        date
        duration
        organizer_email
        meeting_link
        summary {
          short_summary
          action_items
          keywords
        }
        meeting_attendees {
          email
          displayName
        }
      }
    }
    """
    resp = requests.post(
        FIREFLIES_URL,
        json={"query": query, "variables": {"id": transcript_id}},
        headers={"Authorization": f"Bearer {FIREFLIES_API_KEY}", "Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["data"]["transcript"]


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


def find_or_create_contact(email: str, display_name: str | None = None) -> str:
    search = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email"],
            "limit": 1,
        },
        headers=_hs_headers(),
        timeout=10,
    )
    search.raise_for_status()
    results = search.json().get("results", [])
    if results:
        return results[0]["id"]

    props: dict[str, str] = {"email": email}
    if display_name:
        parts = display_name.strip().split(" ", 1)
        props["firstname"] = parts[0]
        if len(parts) > 1:
            props["lastname"] = parts[1]

    create = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        json={"properties": props},
        headers=_hs_headers(),
        timeout=10,
    )
    create.raise_for_status()
    return create.json()["id"]


def push_call_to_hubspot(transcript: dict) -> str:
    duration_ms = int((transcript.get("duration") or 0) * 60 * 1000)

    raw_date = transcript.get("date") or ""
    if raw_date:
        if isinstance(raw_date, (int, float)):
            ts_ms = int(raw_date)
        else:
            dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            ts_ms = int(dt.timestamp() * 1000)
    else:
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Build note body
    parts: list[str] = []
    summary = transcript.get("summary")
    if summary:
        if summary.get("short_summary"):
            parts.append(f"Summary:\n{summary['short_summary']}")
        if summary.get("keywords"):
            parts.append(f"Keywords: {', '.join(summary['keywords'])}")
        if summary.get("action_items"):
            parts.append(f"Action Items:\n{summary['action_items']}")
    if transcript.get("meeting_link"):
        parts.append(f"Recording: {transcript['meeting_link']}")

    call_props = {
        "hs_call_title": transcript.get("title") or "Call",
        "hs_call_body": "\n\n".join(parts) if parts else "No summary available.",
        "hs_call_duration": str(duration_ms),
        "hs_call_status": "COMPLETED",
        "hs_timestamp": str(ts_ms),
        "hs_call_direction": "OUTBOUND",
    }

    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/calls",
        json={"properties": call_props},
        headers=_hs_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    call_id: str = resp.json()["id"]

    attendees = [a for a in (transcript.get("meeting_attendees") or []) if a.get("email")]
    for attendee in attendees:
        contact_id = find_or_create_contact(attendee["email"], attendee.get("displayName"))
        requests.put(
            f"{HUBSPOT_BASE}/crm/v3/objects/calls/{call_id}/associations/contacts/{contact_id}/call_to_contact",
            headers={"Authorization": f"Bearer {HUBSPOT_API_KEY}"},
            timeout=10,
        ).raise_for_status()

    return call_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/fireflies")
async def fireflies_webhook(
    request: Request,
    x_fireflies_signature: str | None = Header(default=None),
):
    payload = await request.body()
    verify_signature(payload, x_fireflies_signature)

    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    event_type: str = body.get("eventType", "")

    # Fireflies fires "Transcription completed" when a meeting is fully processed
    if event_type not in ("Transcription completed", "transcription_completed"):
        return JSONResponse({"status": "ignored", "eventType": event_type})

    transcript_id: str = body.get("meetingId") or body.get("transcriptId", "")
    if not transcript_id:
        raise HTTPException(status_code=400, detail="Missing meetingId / transcriptId")

    transcript = fetch_transcript(transcript_id)
    call_id = push_call_to_hubspot(transcript)

    return JSONResponse(
        {"status": "synced", "transcriptId": transcript_id, "hubspotCallId": call_id}
    )


if __name__ == "__main__":
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8000, reload=True)
