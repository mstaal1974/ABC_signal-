"""HubSpot integration — contact ownership handoff + meeting-link sharing.

Scoped to a minimal Service Key (formerly known as Private App). Only the
following scopes are required:

    crm.objects.contacts.read
    crm.objects.contacts.write
    crm.objects.companies.read
    crm.objects.companies.write
    crm.objects.owners.read
    scheduler.meetings.meeting-link.read

What this module does:
- `find_or_create_contact(...)` / `find_or_create_company(...)` — same as
  before; sets up the CRM records the rest hangs off.
- `set_contact_owner(contact_id, owner_id)` — updates the contact's
  hubspot_owner_id property. This is the handoff signal: the contact then
  appears in the BD's "assigned to me" view. Native HubSpot pattern.
- `get_meeting_link_for_owner(owner_id)` — looks up the BD's HubSpot
  Meetings booking link (their Calendly-equivalent). The salesperson shares
  this link with the prospect; the prospect picks a slot from the BD's
  real availability; HubSpot auto-creates the Appointment when they book.
- `sync_to_hubspot(...)` — one-shot orchestrator that does contact + company
  find-or-create, contact-to-company association, BD ownership assignment,
  and meeting-link lookup.

secrets.toml schema:
    [hubspot]
    private_app_token = "pat-ap1-..."     # APAC tokens start with pat-ap1-
    portal_id         = "12345678"

Failure modes: every function returns None / False / a result dict rather
than raising. The orchestrator captures HubSpot's exact error message
(including requiredGranularScopes hint) so the UI can show it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx
import streamlit as st


BASE = "https://api.hubapi.com"
TIMEOUT = 20.0

# Last error response from a failed _request(); the orchestrator reads this
# to surface HubSpot's required-scope hint when something fails.
_last_error: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _config() -> Dict[str, str]:
    try:
        return dict(st.secrets.get("hubspot", {}))
    except Exception:
        return {}


def _get_token() -> Optional[str]:
    return _config().get("private_app_token") or None


def is_configured() -> bool:
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
    if not (portal and contact_id):
        return ""
    return f"https://app.hubspot.com/contacts/{portal}/contact/{contact_id}"


def company_url(company_id: str) -> str:
    portal = _portal_id()
    if not (portal and company_id):
        return ""
    return f"https://app.hubspot.com/contacts/{portal}/company/{company_id}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Single HTTP helper that populates _last_error on failure so callers
    can surface HubSpot's exact error message."""
    global _last_error
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            resp = c.request(
                method,
                f"{BASE}{path}",
                headers=_headers(),
                json=payload if payload is not None else None,
            )
    except (httpx.RequestError, httpx.HTTPError) as e:
        _last_error = {"_error": {"message": f"Network error: {e}"}}
        return None
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {"message": resp.text[:300]}
        _last_error = {"_error": err, "_status": resp.status_code}
        return {"_error": err, "_status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        # PATCH on properties sometimes returns 204 No Content with no body.
        return {} if resp.status_code < 300 else None


def _post(path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _request("POST", path, payload)


def _get(path: str) -> Optional[Dict[str, Any]]:
    return _request("GET", path)


def _patch(path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _request("PATCH", path, payload)


def _put_no_body(path: str) -> bool:
    """v4 associations PUT — takes no body, returns 200/204."""
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            resp = c.put(f"{BASE}{path}", headers=_headers())
    except (httpx.RequestError, httpx.HTTPError):
        return False
    return resp.status_code < 400


# ---------------------------------------------------------------------------
# Owners (BDs) — cached per session
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner=False)
def list_owners() -> List[Dict[str, str]]:
    """Return all HubSpot users available to be assigned as owners."""
    if not is_configured():
        return []
    out: List[Dict[str, str]] = []
    after: Optional[str] = None
    for _ in range(5):
        path = "/crm/v3/owners/?limit=100"
        if after:
            path += f"&after={after}"
        data = _get(path)
        if not data or "_error" in data:
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
    if not email or "@" not in email:
        return None

    search = _post(
        "/crm/v3/objects/contacts/search",
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email"],
            "limit": 1,
        },
    )
    if search and "_error" not in search and search.get("results"):
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
    if not name:
        return None

    search = _post(
        "/crm/v3/objects/companies/search",
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "name", "operator": "EQ", "value": name}
                    ]
                }
            ],
            "properties": ["name"],
            "limit": 1,
        },
    )
    if search and "_error" not in search and search.get("results"):
        return search["results"][0]["id"]

    created = _post("/crm/v3/objects/companies", {"properties": {"name": name}})
    if created and "id" in created:
        return created["id"]
    return None


def associate(from_type: str, from_id: str, to_type: str, to_id: str) -> bool:
    """Default association via v4 — no need to know association type IDs."""
    if not (from_id and to_id):
        return False
    return _put_no_body(
        f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
    )


# ---------------------------------------------------------------------------
# Ownership transfer (the handoff)
# ---------------------------------------------------------------------------


def set_contact_owner(contact_id: str, owner_id: str) -> bool:
    """Assign a BD as the contact's owner. This is the native HubSpot handoff:
    the contact now shows up in the BD's 'My contacts' / assigned filters."""
    if not (contact_id and owner_id):
        return False
    resp = _patch(
        f"/crm/v3/objects/contacts/{contact_id}",
        {"properties": {"hubspot_owner_id": owner_id}},
    )
    return resp is not None and "_error" not in resp


# ---------------------------------------------------------------------------
# Meeting link lookup
# ---------------------------------------------------------------------------


def get_meeting_link_for_owner(owner_id: str) -> Optional[Dict[str, str]]:
    """Find the BD's HubSpot Meetings booking link.

    Returns {"name": ..., "link": ..., "type": ...} or None if the BD has no
    meeting link configured.

    Prefers the BD's PERSONAL link; falls back to any link they're a member
    of (e.g. a round-robin link that includes them).
    """
    if not owner_id:
        return None

    # Look up the owner's user_id so we can match against organizerUserId
    # / userIdsOfLinkMembers fields on meeting links.
    owner = _get(f"/crm/v3/owners/{owner_id}")
    if not owner or "_error" in owner:
        return None
    user_id = str(owner.get("userId", "")) or owner_id

    links = _get("/scheduler/v3/meetings/meeting-links?limit=100")
    if not links or "_error" in links:
        return None

    personal: Optional[Dict[str, str]] = None
    member: Optional[Dict[str, str]] = None
    for link in links.get("results", []) or []:
        organizer = str(link.get("organizerUserId", ""))
        members = [str(m) for m in (link.get("userIdsOfLinkMembers", []) or [])]
        link_type = (link.get("type") or "").upper()
        info = {
            "name": link.get("name", "") or "",
            "link": link.get("link", "") or "",
            "type": link_type,
            "slug": link.get("slug", "") or "",
        }
        if organizer == user_id and link_type == "PERSONAL":
            return info  # exact match — best
        if organizer == user_id:
            personal = personal or info
        elif user_id in members:
            member = member or info

    return personal or member


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
) -> Dict[str, Any]:
    """One-shot: contact + company + association + owner assignment + meeting link.

    Returns:
        {
          "contact_id":   str | None,
          "contact_url":  str | None,
          "company_id":   str | None,
          "company_url":  str | None,
          "owner_set":    bool,
          "meeting_link": {"name", "link", "type"} | None,
          "errors":       [str, ...],
        }
    """
    result: Dict[str, Any] = {
        "contact_id": None,
        "contact_url": None,
        "company_id": None,
        "company_url": None,
        "owner_set": False,
        "meeting_link": None,
        "errors": [],
    }

    if not is_configured():
        result["errors"].append(
            "HubSpot is not configured (check [hubspot] in secrets.toml)."
        )
        return result
    if not bd_owner_id:
        result["errors"].append(
            "No BD selected — pick an owner before handing off."
        )
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
        err_msg = _scope_or_message_hint("contact")
        result["errors"].append(f"Couldn't find or create the contact — {err_msg}")
        return result
    result["contact_id"] = contact_id
    result["contact_url"] = contact_url(contact_id)

    # 2. Company (optional)
    company_id: Optional[str] = None
    if company_name:
        company_id = find_or_create_company(name=company_name)
        if company_id:
            result["company_id"] = company_id
            result["company_url"] = company_url(company_id)
            if not associate("contacts", contact_id, "companies", company_id):
                result["errors"].append(
                    "Contact/company association failed (non-fatal)."
                )
        else:
            result["errors"].append(
                f"Couldn't find or create the company — {_scope_or_message_hint('company')}"
            )

    # 3. Assign the BD as contact owner (the handoff)
    if set_contact_owner(contact_id, bd_owner_id):
        result["owner_set"] = True
    else:
        result["errors"].append(
            f"Couldn't assign the BD as contact owner — {_scope_or_message_hint('owner assignment')}"
        )

    # 4. Look up the BD's meeting link
    link = get_meeting_link_for_owner(bd_owner_id)
    if link:
        result["meeting_link"] = link
    else:
        # Not necessarily an error — the BD might just not have a personal
        # meetings link set up in HubSpot yet.
        result["errors"].append(
            "No HubSpot Meetings link found for this BD. They may need to "
            "create one in HubSpot > Sales > Meetings (or the scope "
            "scheduler.meetings.meeting-link.read may be missing)."
        )

    return result


def _scope_or_message_hint(label: str) -> str:
    """Pull HubSpot's required-scope hint or error message from _last_error."""
    if not _last_error:
        return f"HubSpot returned no detail for {label}."
    err = _last_error.get("_error") or {}
    missing = (
        (err.get("context") or {}).get("requiredGranularScopes")
        or err.get("requiredGranularScopes")
    )
    if missing:
        return f"missing Service Key scope(s): {', '.join(missing)}"
    return err.get("message") or f"unknown error for {label}."


# ---------------------------------------------------------------------------
# Backwards-compatible aliases (kept so the rest of the codebase doesn't break
# if older callers still reference these names — they're no-ops).
# ---------------------------------------------------------------------------


def contact_owner_url(*_args, **_kwargs) -> str:
    return ""
