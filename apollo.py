"""Apollo.io adapter for contact discovery and email reveal.

Two-tier API design that maps to Apollo's two-tier pricing:

- `find_contacts(company, vertical)` uses Apollo's **People Search** endpoint,
  which is FREE (does NOT consume credits). Returns candidate contacts at
  the company filtered to titles relevant for the vertical. No emails or
  phone numbers — Apollo deliberately holds those back for the paid path.

- `reveal_email(apollo_id)` uses **People Enrichment**, which CONSUMES one
  credit per successful match. Returns the verified email if Apollo has one.
  Should only be called on user demand (a "reveal email" button in the UI),
  not auto-fired during enrichment.

Cost model:
- People Search: free, but People Search REQUIRES a master API key. Create
  it in Apollo: Settings > Integrations > API > Master Key.
- People Enrichment: free tier ~1 credit/day. Paid plans start ~$50/user/mo
  with several hundred credits/month.

AU coverage caveat:
Apollo's AU/NZ data is reasonable for mid-large companies (~100+ employees)
and thins out quickly below that. A small specialty Australian lab may
return zero contacts. Not a bug — just thin source data.

Compliance reminder:
Apollo gives you contact information, NOT consent to email. Australian
Spam Act 2003 still applies to anything you do with the data — express or
inferred consent required, working unsubscribe required. B2B inferred
consent for senior decision-makers in directly-relevant roles is generally
defensible but is a judgment per send. This adapter surfaces contacts to a
human salesperson; it does not drive bulk send.

Failure modes:
Every function returns a sensible default (empty list, None) on any failure
— missing API key, network error, non-200 response, malformed JSON. The
enrichment pass treats Apollo as an optional augmentation; if it goes
sideways, the rest of the signal still works.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import streamlit as st


APOLLO_BASE = "https://api.apollo.io/api/v1"
APOLLO_TIMEOUT = 15.0


# Title taxonomies per vertical. Apollo's People Search supports an array of
# titles with OR semantics. These are the roles a sales rep at an RTO actually
# cares about — operational leads, quality/HR managers, hiring decision-makers
# — not marketers or unrelated execs.
VERTICAL_TITLES: Dict[str, List[str]] = {
    "Laboratory": [
        "Laboratory Manager",
        "Lab Manager",
        "Quality Manager",
        "QA Manager",
        "QC Manager",
        "Operations Manager",
        "Technical Manager",
        "Lab Director",
        "Senior Chemist",
        "HR Manager",
    ],
    "CMT": [
        "Quality Manager",
        "Materials Manager",
        "Senior Materials Engineer",
        "Geotechnical Manager",
        "Geotechnical Engineer",
        "Project Manager",
        "Construction Manager",
        "Operations Manager",
        "Site Manager",
        "HR Manager",
    ],
    "Pathology": [
        "Laboratory Manager",
        "Pathology Manager",
        "Operations Manager",
        "Quality Manager",
        "Chief Scientist",
        "Senior Medical Scientist",
        "Collections Manager",
        "HR Manager",
    ],
    "Manufacturing": [
        "Plant Manager",
        "Production Manager",
        "Operations Manager",
        "Manufacturing Manager",
        "Quality Manager",
        "Continuous Improvement Manager",
        "HR Manager",
        "Talent Acquisition Manager",
        "People and Culture Manager",
    ],
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _get_api_key() -> Optional[str]:
    """Return the Apollo master API key from secrets, or None if not set."""
    try:
        keys = st.secrets.get("api_keys", {})
        return keys.get("apollo") or None
    except Exception:
        return None


def is_configured() -> bool:
    """True if Apollo is enabled (master API key present in secrets.toml)."""
    return bool(_get_api_key())


# ---------------------------------------------------------------------------
# People Search (free — does not consume credits)
# ---------------------------------------------------------------------------


def find_contacts(
    company_name: str,
    vertical: str,
    country: str = "Australia",
    per_page: int = 10,
) -> List[Dict[str, Any]]:
    """Search Apollo for relevant contacts at a company.

    Returns a list of contact dicts with name, title, LinkedIn URL, Apollo
    ID, and seniority. Email is NOT returned by this endpoint — use
    reveal_email() to get that, which consumes a credit.

    Returns [] on any failure mode — missing key, network error, no matches.
    """
    api_key = _get_api_key()
    if not api_key or not company_name:
        return []

    titles = VERTICAL_TITLES.get(vertical, [])

    payload: Dict[str, Any] = {
        "q_organization_name": company_name,
        "person_titles": titles,
        "person_locations": [country] if country else [],
        "per_page": per_page,
        "page": 1,
    }

    try:
        with httpx.Client(timeout=APOLLO_TIMEOUT) as client:
            resp = client.post(
                f"{APOLLO_BASE}/people/search",
                headers={
                    "x-api-key": api_key,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json=payload,
            )
    except (httpx.RequestError, httpx.HTTPError):
        return []

    if resp.status_code != 200:
        return []

    try:
        data = resp.json()
    except ValueError:
        return []

    people = data.get("people", []) or []
    out: List[Dict[str, Any]] = []
    for p in people:
        name = p.get("name") or _join_name(p)
        if not name:
            continue
        out.append(
            {
                "name": name,
                "title": p.get("title", ""),
                "linkedin_url": p.get("linkedin_url", ""),
                "apollo_id": p.get("id", ""),
                "seniority": p.get("seniority", ""),
                "city": p.get("city", ""),
                "email": None,
                "email_revealed": False,
                "source": "apollo",
            }
        )
    return out


# ---------------------------------------------------------------------------
# People Enrichment (CONSUMES 1 CREDIT per successful match)
# ---------------------------------------------------------------------------


def reveal_email(apollo_id: str) -> Optional[str]:
    """Reveal the verified email for an Apollo contact. CONSUMES 1 CREDIT.

    Returns None if the call fails or no email is on file.
    """
    api_key = _get_api_key()
    if not api_key or not apollo_id:
        return None

    try:
        with httpx.Client(timeout=APOLLO_TIMEOUT) as client:
            resp = client.post(
                f"{APOLLO_BASE}/people/match",
                headers={
                    "x-api-key": api_key,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json={
                    "id": apollo_id,
                    "reveal_personal_emails": False,
                },
            )
    except (httpx.RequestError, httpx.HTTPError):
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    person = data.get("person") or {}
    email = person.get("email")
    return email if email and "@" in email else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_name(p: Dict[str, Any]) -> str:
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    return (first + " " + last).strip()
