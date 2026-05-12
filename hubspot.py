"""HubSpot integration — task assignment and meeting booking.

Workflow this supports:
- Salesperson identifies a signal, drafts a sequence, then hands the prospect
  to a BD (business development rep) for follow-up.
- The handoff is captured as a HubSpot **task** assigned to that BD (with the
  drafted email body and phone script as the task body), and/or a HubSpot
  **meeting** scheduled with that BD as owner.
- Contact and company records are find-or-created so everything links cleanly
  in the CRM.

Authentication: Private App token (Bearer). Single HubSpot account.

Required HubSpot configuration BEFORE this works:
1. A Private App with scopes:
     crm.objects.contacts.read, crm.objects.contacts.write
     crm.objects.companies.read, crm.objects.companies.write
     crm.objects.owners.read
     crm.objects.tasks.write
     crm.objects.meetings.write
2. The BDs you want to assign work to must exist as Users in HubSpot
   (Settings > Users & Teams).

Subscription requirement: works on all HubSpot tiers including free.
No Marketing Hub, no Transactional Email add-on, no email template.

secrets.toml schema:
    [hubspot]
    private_app_token = "pat-na1-..."
    portal_id         = "12345678"

Failure modes: every function returns None / False / a result dict rather than
raising. The UI surfaces partial-success states so the salesperson knows what
made it into HubSpot and what didn't.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import httpx
import streamlit as st


BASE = "https://api.hubapi.com"
TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _config() -> Dict[str, str]:
    """Return [hubspot] config as a dict (empty if not configured)."""
    try:
        return dict(st.secrets.get("hubspot", {}))
    except Exception:
        return {}


def _get_token() -> Optional[str]:
    return _config().get("private_app_token") or None


def is_configured() -> bool:
    """True if a Private App token is present in secrets."""
    return bool(_get_token())


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _portal_id() -> str:
    return _config().get("portal_id", "")


def contact_url(contact_id: str) -> str:
    portal = _portal_id()
    if not portal or not contact_id:
        return ""
    return f"https://app.hubspot.com/contacts/{portal}/contact/{contact_id}"


def company_url(company_id: str) -> str:
    portal = _portal_id()
    if not portal or not company_id:
        return ""
    return f"https://app.hubspot.com/contacts/{portal}/company/{company_id}"


def task_url(task_id: str) -> str:
    portal = _portal_id()
    if not portal or not task_id:
        return ""
    return f"https://app.hubspot.com/contacts/{portal}/record/0-27/{task_id}"


def meeting_url(meeting_id: str) -> str:
    portal = _portal_id()
    if not portal or not meeting_id:
        return ""
    return f"https://app.hubspot.com/contacts/{portal}/record/0-47/{meeting_id}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post(path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST to HubSpot; return parsed JSON or None on any failure."""
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            resp = c.post(f"{BASE}{path}", headers=_headers(), json=payload)
    except (httpx.RequestError, httpx.HTTPError):
        return None
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {"message": resp.text[:300]}
        return {"_error": err, "_status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        return None


def _get(path: str) -> Optional[Dict[str, Any]]:
    """GET from HubSpot; return parsed JSON or None on any failure."""
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            resp = c.get(f"{BASE}{path}", headers=_headers())
    except (httpx.RequestError, httpx.HTTPError):
        return None
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _put(path: str) -> bool:
    """PUT to HubSpot (associations endpoint takes no body); return success."""
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            resp = c.put(f"{BASE}{path}", headers=_headers())
    except (httpx.RequestError, httpx.HTTPError):
        return False
    return resp.status_code < 400


# ---------------------------------------------------------------------------
# Owners (BDs) — cached per-session
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner=False)
def list_owners() -> List[Dict[str, str]]:
    """Return all HubSpot users available to be assigned as owners. Cached
    for 5 minutes since the BD roster changes rarely.

    Returns list of dicts: {"id": str, "email": str, "name": str}
    """
    if not is_configured():
        return []

    out: List[Dict[str, str]] = []
    after: Optional[str] = None
    # Paginate (limit 100 per page, max ~5 pages — plenty for any RTO).
    for _ in range(5):
        path = "/crm/v3/owners/?limit=100"
        if after:
            path += f"&after={after}"
        data = _get(path)
        if not data:
            break
        for o in data.get("results", []) or []:
            first = o.get("firstName") or ""
            last = o.get("lastName") or ""
            name = (first + " " + last).strip() or o.get("email", "")
            out.append(
                {
                    "id": str(o.get("id", "")),
                    "email": o.get("email", "") or "",
                    "name": name,
                }
            )
        paging = (data.get("paging") or {}).get("next") or {}
        after = paging.get("after")
        if not after:
            break
    return out


# ---------------------------------------------------------------------------
# Contacts and companies (find-or-create)
# ---------------------------------------------------------------------------


def find_or_create_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    job_title: str = "",
    company_name: str = "",
) -> Optional[str]:
    """Find a contact by email, or create one. Returns contact_id or None."""
    if not email or "@" not in email:
        return None

    search = _post(
        "/crm/v3/objects/contacts/search",
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            "properties": ["email"],
            "limit": 1,
        },
    )
    if search and "results" in search and search["results"]:
        return search["results"][0]["id"]

    props: Dict[str, str] = {"email": email}
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if job_title:
        props["jobtitle"] = job_title
    if company_name:
        props["company"] = company_name

    created = _post("/crm/v3/objects/contacts", {"properties": props})
    if created and "id" in created:
        return created["id"]
    return None


def find_or_create_company(name: str) -> Optional[str]:
    """Find a company by name, or create one. Returns company_id or None."""
    if not name:
        return None

    search = _post(
        "/crm/v3/objects/companies/search",
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "name",
                            "operator": "EQ",
                            "value": name,
                        }
                    ]
                }
            ],
            "properties": ["name"],
            "limit": 1,
        },
    )
    if search and "results" in search and search["results"]:
        return search["results"][0]["id"]

    created = _post("/crm/v3/objects/companies", {"properties": {"name": name}})
    if created and "id" in created:
        return created["id"]
    return None


def associate(
    from_type: str, from_id: str, to_type: str, to_id: str
) -> bool:
    """Create a default association between two CRM objects via the v4 API.

    Using the 'default' endpoint avoids needing to know HubSpot's internal
    association type IDs (which vary by object pair and aren't always stable
    to memorise).
    """
    if not (from_id and to_id):
        return False
    return _put(
        f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def create_task(
    owner_id: str,
    subject: str,
    body: str,
    due_at_iso: Optional[str] = None,
    priority: str = "MEDIUM",
    task_type: str = "TODO",
    contact_id: Optional[str] = None,
    company_id: Optional[str] = None,
) -> Optional[str]:
    """Create a HubSpot task assigned to the given owner (BD).

    Args:
        owner_id: HubSpot user ID of the BD the task is assigned to.
        subject: Task title.
        body: Task body / notes — typically the drafted email + phone script.
        due_at_iso: ISO-8601 timestamp for when the task is due. Defaults to
            24 hours from now.
        priority: LOW | MEDIUM | HIGH.
        task_type: TODO | CALL | EMAIL.
        contact_id: optional contact to associate.
        company_id: optional company to associate.

    Returns the task_id, or None on failure.
    """
    if not owner_id or not subject:
        return None

    timestamp_ms = _iso_to_ms(due_at_iso) if due_at_iso else _default_due_ms()

    properties: Dict[str, Any] = {
        "hs_task_subject": subject,
        "hs_task_body": body or "",
        "hs_task_status": "NOT_STARTED",
        "hs_task_priority": priority,
        "hs_task_type": task_type,
        "hs_timestamp": str(timestamp_ms),
        "hubspot_owner_id": owner_id,
    }

    created = _post("/crm/v3/objects/tasks", {"properties": properties})
    if not (created and "id" in created):
        return None

    task_id = created["id"]
    # Associate after creation — simpler than embedding association type IDs.
    if contact_id:
        associate("tasks", task_id, "contacts", contact_id)
    if company_id:
        associate("tasks", task_id, "companies", company_id)
    return task_id


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


def create_meeting(
    owner_id: str,
    title: str,
    body: str,
    start_iso: str,
    duration_minutes: int = 30,
    contact_id: Optional[str] = None,
    company_id: Optional[str] = None,
    location: str = "",
) -> Optional[str]:
    """Create a HubSpot meeting record owned by the given BD.

    Note: this creates the meeting record in HubSpot, which appears in the
    BD's HubSpot timeline. It does NOT automatically send calendar invites —
    that depends on the BD's HubSpot ↔ Google/Outlook calendar integration,
    which is configured per user in HubSpot Settings, not via this API.

    Returns the meeting_id, or None on failure.
    """
    if not owner_id or not title or not start_iso:
        return None

    start_ms = _iso_to_ms(start_iso)
    if start_ms is None:
        return None
    end_ms = start_ms + (max(duration_minutes, 5) * 60 * 1000)

    properties: Dict[str, Any] = {
        "hs_timestamp": str(start_ms),
        "hs_meeting_title": title,
        "hs_meeting_body": body or "",
        "hs_meeting_start_time": str(start_ms),
        "hs_meeting_end_time": str(end_ms),
        "hs_meeting_outcome": "SCHEDULED",
        "hubspot_owner_id": owner_id,
    }
    if location:
        properties["hs_meeting_location"] = location

    created = _post("/crm/v3/objects/meetings", {"properties": properties})
    if not (created and "id" in created):
        return None

    meeting_id = created["id"]
    if contact_id:
        associate("meetings", meeting_id, "contacts", contact_id)
    if company_id:
        associate("meetings", meeting_id, "companies", company_id)
    return meeting_id


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _iso_to_ms(iso: str) -> Optional[int]:
    """Convert an ISO-8601 timestamp to milliseconds since epoch."""
    if not iso:
        return None
    try:
        # struct_time path keeps this dependency-free; assumes UTC if no tz.
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        # Use fromisoformat via datetime — imported lazily for clarity.
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _default_due_ms() -> int:
    """24 hours from now, in ms since epoch."""
    return int((time.time() + 86400) * 1000)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def sync_to_hubspot(
    *,
    recipient_email: str,
    recipient_first: str = "",
    recipient_last: str = "",
    recipient_title: str = "",
    company_name: str = "",
    bd_owner_id: str,
    create_task_opts: Optional[Dict[str, Any]] = None,
    create_meeting_opts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One-shot: contact + company + (optional task) + (optional meeting).

    `create_task_opts` should be a dict with keys matching create_task() kwargs
    (subject, body, due_at_iso, priority, task_type), or None to skip.

    `create_meeting_opts` should be a dict with keys matching create_meeting()
    kwargs (title, body, start_iso, duration_minutes, location), or None to skip.

    Returns a structured result dict; never raises. Caller inspects which
    steps succeeded.
    """
    result: Dict[str, Any] = {
        "contact_id": None,
        "contact_url": None,
        "company_id": None,
        "company_url": None,
        "task_id": None,
        "task_url": None,
        "meeting_id": None,
        "meeting_url": None,
        "errors": [],
    }

    if not is_configured():
        result["errors"].append("HubSpot is not configured (check [hubspot] in secrets.toml).")
        return result
    if not bd_owner_id:
        result["errors"].append("No BD selected — pick an owner before assigning work.")
        return result

    # 1. Contact
    contact_id = find_or_create_contact(
        email=recipient_email,
        first_name=recipient_first,
        last_name=recipient_last,
        job_title=recipient_title,
        company_name=company_name,
    )
    if not contact_id:
        result["errors"].append("Couldn't find or create the contact.")
        return result
    result["contact_id"] = contact_id
    result["contact_url"] = contact_url(contact_id)

    # 2. Company
    company_id: Optional[str] = None
    if company_name:
        company_id = find_or_create_company(name=company_name)
        if company_id:
            result["company_id"] = company_id
            result["company_url"] = company_url(company_id)
            if not associate("contacts", contact_id, "companies", company_id):
                result["errors"].append("Contact/company association failed (non-fatal).")
        else:
            result["errors"].append("Couldn't find or create the company (non-fatal).")

    # 3. Task (optional)
    if create_task_opts:
        task_id = create_task(
            owner_id=bd_owner_id,
            contact_id=contact_id,
            company_id=company_id,
            **create_task_opts,
        )
        if task_id:
            result["task_id"] = task_id
            result["task_url"] = task_url(task_id)
        else:
            result["errors"].append("Couldn't create the task.")

    # 4. Meeting (optional)
    if create_meeting_opts:
        meeting_id = create_meeting(
            owner_id=bd_owner_id,
            contact_id=contact_id,
            company_id=company_id,
            **create_meeting_opts,
        )
        if meeting_id:
            result["meeting_id"] = meeting_id
            result["meeting_url"] = meeting_url(meeting_id)
        else:
            result["errors"].append("Couldn't create the meeting.")

    return result
