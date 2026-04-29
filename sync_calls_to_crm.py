"""
Fireflies → HubSpot CRM sync.

Usage:
    python sync_calls_to_crm.py            # sync all unsynced meetings
    python sync_calls_to_crm.py --limit 10 # sync last N meetings
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
STATE_FILE = Path("synced_meetings.json")

FIREFLIES_URL = "https://api.fireflies.ai/graphql"
HUBSPOT_BASE = "https://api.hubapi.com"

_HEADERS_FF = lambda: {"Authorization": f"Bearer {FIREFLIES_API_KEY}", "Content-Type": "application/json"}
_HEADERS_HS = lambda: {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Fireflies helpers
# ---------------------------------------------------------------------------

def get_transcripts(limit: int = 50) -> list[dict]:
    query = """
    query Transcripts($limit: Int) {
      transcripts(limit: $limit) {
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
        meeting_info {
          summary_status
        }
      }
    }
    """
    resp = requests.post(
        FIREFLIES_URL,
        json={"query": query, "variables": {"limit": limit}},
        headers=_HEADERS_FF(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Fireflies error: {data['errors']}")
    return data["data"]["transcripts"]


def get_transcript_detail(transcript_id: str) -> dict:
    """Fetch full transcript content for a single meeting."""
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
        meeting_info {
          summary_status
        }
      }
    }
    """
    resp = requests.post(
        FIREFLIES_URL,
        json={"query": query, "variables": {"id": transcript_id}},
        headers=_HEADERS_FF(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["transcript"]


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def find_contact_by_email(email: str) -> str | None:
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email"],
            "limit": 1,
        },
        headers=_HEADERS_HS(),
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def create_contact(email: str, display_name: str | None = None) -> str:
    props: dict[str, str] = {"email": email}
    if display_name:
        parts = display_name.strip().split(" ", 1)
        props["firstname"] = parts[0]
        if len(parts) > 1:
            props["lastname"] = parts[1]
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        json={"properties": props},
        headers=_HEADERS_HS(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def find_or_create_contact(email: str, display_name: str | None = None) -> str:
    contact_id = find_contact_by_email(email)
    if contact_id:
        return contact_id
    print(f"    Creating new contact: {email}")
    return create_contact(email, display_name)


def build_call_body(transcript: dict) -> str:
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
    return "\n\n".join(parts) if parts else "No summary available."


def create_hubspot_call(transcript: dict, contact_ids: list[str]) -> str:
    duration_ms = int((transcript.get("duration") or 0) * 60 * 1000)

    raw_date = transcript.get("date") or transcript.get("dateString") or ""
    if raw_date:
        # Fireflies returns epoch ms or ISO string
        if isinstance(raw_date, (int, float)):
            ts_ms = int(raw_date)
        else:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            ts_ms = int(dt.timestamp() * 1000)
    else:
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    call_props = {
        "hs_call_title": transcript.get("title") or "Call",
        "hs_call_body": build_call_body(transcript),
        "hs_call_duration": str(duration_ms),
        "hs_call_status": "COMPLETED",
        "hs_timestamp": str(ts_ms),
        "hs_call_direction": "OUTBOUND",
    }

    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/calls",
        json={"properties": call_props},
        headers=_HEADERS_HS(),
        timeout=15,
    )
    resp.raise_for_status()
    call_id: str = resp.json()["id"]

    for contact_id in contact_ids:
        requests.put(
            f"{HUBSPOT_BASE}/crm/v3/objects/calls/{call_id}/associations/contacts/{contact_id}/call_to_contact",
            headers={k: v for k, v in _HEADERS_HS().items() if k != "Content-Type"},
            timeout=10,
        ).raise_for_status()

    return call_id


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"synced_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(limit: int = 50, force: bool = False) -> None:
    if not FIREFLIES_API_KEY:
        sys.exit("Error: FIREFLIES_API_KEY not set")
    if not HUBSPOT_API_KEY:
        sys.exit("Error: HUBSPOT_API_KEY not set")

    state = load_state()
    synced_ids: set[str] = set() if force else set(state["synced_ids"])

    print(f"Fetching up to {limit} Fireflies transcripts…")
    transcripts = get_transcripts(limit=limit)
    print(f"Found {len(transcripts)} transcripts ({len(synced_ids)} already synced)\n")

    new_syncs = 0
    for t in transcripts:
        tid: str = t["id"]
        if tid in synced_ids:
            continue

        title = t.get("title") or tid
        attendees = [a for a in (t.get("meeting_attendees") or []) if a.get("email")]
        if not attendees:
            print(f"  skip  {title} – no attendees")
            synced_ids.add(tid)
            continue

        print(f"  sync  {title}")

        contact_ids = []
        for attendee in attendees:
            cid = find_or_create_contact(attendee["email"], attendee.get("displayName"))
            contact_ids.append(cid)

        call_id = create_hubspot_call(t, contact_ids)
        print(f"        → HubSpot call {call_id} linked to {len(contact_ids)} contact(s)")

        synced_ids.add(tid)
        new_syncs += 1

    state["synced_ids"] = list(synced_ids)
    save_state(state)
    print(f"\nDone – {new_syncs} new meeting(s) pushed to HubSpot.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Fireflies call recordings to HubSpot CRM")
    parser.add_argument("--limit", type=int, default=50, help="Max transcripts to fetch (default 50)")
    parser.add_argument("--force", action="store_true", help="Re-sync already-synced meetings")
    args = parser.parse_args()
    sync(limit=args.limit, force=args.force)
