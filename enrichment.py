"""Signal enrichment pass.

After Gemini's scan returns shallow signals (headline + snippet), this module
fetches each signal's source article, extracts deeper operational detail via
Claude Haiku 4.5, and returns enriched signals ready for the sequence drafter.

Why a separate enrichment pass:
- Gemini's grounded scan gives a headline and 2-3 sentence snippet per signal.
  Enough to draft an opener, not enough to qualify the signal or target a
  specific person.
- Fetching the actual article and running structured extraction gives the
  sequence drafter source-verified detail: contract value, project duration,
  named individuals, the specific operational problem the signal creates,
  technical skills implied by the work.
- Claude Haiku 4.5 is the right model for this — fast, cheap, and very good
  at structured extraction from longish text. Sonnet/Opus would be overkill.

Concurrency: signals are enriched in parallel via ThreadPoolExecutor. 8 signals
at ~3s each becomes ~4s wall time instead of 24s. The Anthropic SDK is
thread-safe; one client instance is shared across workers.

Failure modes: every signal returns. If the URL can't be fetched, the signal
comes back with fetch_status='fetch_failed' and its original fields intact —
nothing is dropped. The UI surfaces fetch_status so the salesperson knows
whether a card is shallow (headline only) or deep (source-verified).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import anthropic
import httpx
import trafilatura

from prompts import ENRICHMENT_PROMPT, MANUAL_SIGNAL_PROMPT

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Trim fetched articles before sending to Claude. ~6000 chars ≈ 1500 tokens,
# plenty for any real news article body and keeps cost predictable.
MAX_ARTICLE_CHARS = 6000

# Conservative browser-like User-Agent. "python-httpx/x.y" gets blocked by
# many publisher sites; a normal-looking UA gets through. We identify
# ourselves honestly via the trailing product token rather than impersonating
# a logged-in user.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "ABCSignalsBot/1.0 (RTO compliance research)"
)

# Articles that don't return in 10s are usually JS-heavy walls we couldn't
# read anyway. Don't block the rest of the batch waiting on them.
FETCH_TIMEOUT = 10.0


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json(text: str) -> str:
    """Strip optional markdown fences from JSON text; fall back to first { .. last }."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]
    return text


# --- Fetch ---


def fetch_article(url: str) -> Optional[str]:
    """Fetch a URL and return clean main-text article body. None on any failure."""
    if not url or not url.startswith(("http://", "https://")):
        return None

    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        ) as client:
            resp = client.get(url)
    except (httpx.RequestError, httpx.HTTPError):
        return None

    if resp.status_code >= 400:
        return None

    # Skip non-HTML responses (PDFs, JSON APIs) for now — would need different
    # handling per type. Most signal source URLs are HTML news/press release pages.
    ctype = resp.headers.get("content-type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        return None

    extracted = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
    )
    if not extracted or len(extracted) < 100:
        # Too short to be a real article — likely a stub or JS-only page.
        return None

    return extracted[:MAX_ARTICLE_CHARS]


# --- Extract ---


def extract_detail(
    client: anthropic.Anthropic,
    signal: Dict[str, Any],
    article_text: str,
) -> Dict[str, Any]:
    """Run Claude Haiku over the article + signal and return the enrichment dict."""
    prompt = ENRICHMENT_PROMPT.format(
        signal_json=json.dumps(signal, indent=2),
        article_body=article_text,
    )

    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1000,
        temperature=0.1,  # extraction, not creativity
        messages=[{"role": "user", "content": prompt}],
    )

    text = _strip_json(response.content[0].text or "")
    return json.loads(text)


# --- Orchestration ---


def _empty_enrichment(status: str, note: str = "") -> Dict[str, Any]:
    """Stub enrichment fields used when the pass couldn't run end-to-end."""
    return {
        "contract_value": "unknown",
        "project_duration": "unknown",
        "named_individuals": [],
        "team_size_estimate": "unknown",
        "operational_problem": "",
        "skills_implied": [],
        "geographic_footprint": "unknown",
        "confidence": "low",
        "fetch_status": status,
        "fetch_note": note,
    }


def _enrich_one(
    client: anthropic.Anthropic,
    signal: Dict[str, Any],
    apollo_vertical: Optional[str] = None,
) -> Dict[str, Any]:
    """Enrich a single signal. Never raises — failures encoded in fetch_status.

    If `apollo_vertical` is provided, also query Apollo for candidate contacts
    at the company and attach them as `apollo_contacts`. Apollo failures
    silently degrade — the signal still returns with the article-extracted
    fields intact.
    """
    url = signal.get("source_url", "")
    if not url:
        return {
            **signal,
            **_empty_enrichment("no_url", "Signal had no source URL."),
            "apollo_contacts": [],
        }

    article = fetch_article(url)
    if article is None:
        result = {
            **signal,
            **_empty_enrichment(
                "fetch_failed",
                "Couldn't fetch or parse the source (paywall, 404, JS-only, or blocked).",
            ),
        }
        # Still attempt Apollo even if article fetch failed — the signal
        # has a company name from Gemini, which is enough to look up contacts.
        result["apollo_contacts"] = _safe_apollo_lookup(
            signal.get("company", ""), apollo_vertical
        )
        return result

    try:
        enrichment = extract_detail(client, signal, article)
    except (json.JSONDecodeError, anthropic.APIError, KeyError, IndexError) as e:
        return {
            **signal,
            **_empty_enrichment(
                "extract_failed",
                f"Article fetched but extraction failed: {type(e).__name__}",
            ),
            "apollo_contacts": _safe_apollo_lookup(
                signal.get("company", ""), apollo_vertical
            ),
        }

    enrichment["fetch_status"] = "ok"
    enrichment["fetch_note"] = ""
    result = {**signal, **enrichment}
    result["apollo_contacts"] = _safe_apollo_lookup(
        signal.get("company", ""), apollo_vertical
    )
    return result


def _safe_apollo_lookup(
    company: str, vertical: Optional[str]
) -> List[Dict[str, Any]]:
    """Call Apollo for contacts at the company. Empty list on any failure or
    if Apollo augmentation wasn't requested."""
    if not vertical or not company:
        return []
    try:
        import apollo  # lazy import — module is optional
        return apollo.find_contacts(company, vertical)
    except Exception:
        return []


def enrich_signals(
    client: anthropic.Anthropic,
    signals: List[Dict[str, Any]],
    max_workers: int = 8,
    apollo_vertical: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Enrich a list of signals in parallel. Preserves original order.

    If `apollo_vertical` is provided AND apollo is configured, each signal
    is also augmented with Apollo contact candidates (free People Search;
    does not consume credits).
    """
    if not signals:
        return []

    results: List[Optional[Dict[str, Any]]] = [None] * len(signals)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_index = {
            ex.submit(_enrich_one, client, sig, apollo_vertical): i
            for i, sig in enumerate(signals)
        }
        for fut in as_completed(future_to_index):
            i = future_to_index[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # belt-and-braces; _enrich_one shouldn't raise
                results[i] = {
                    **signals[i],
                    **_empty_enrichment(
                        "unknown_error",
                        f"Unexpected error: {type(e).__name__}: {e}",
                    ),
                    "apollo_contacts": [],
                }

    return [r for r in results if r is not None]


def status_summary(signals: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count enrichment outcomes across a batch — for the UI status line."""
    counts: Dict[str, int] = {}
    for s in signals:
        status = s.get("fetch_status", "not_run")
        counts[status] = counts.get(status, 0) + 1
    return counts


# --- Manual signal entry (paste-a-URL flow) ---


def build_signal_from_input(
    client: anthropic.Anthropic,
    url: str,
    vertical: str,
    pasted_text: Optional[str] = None,
    augment_with_apollo: bool = False,
) -> Dict[str, Any]:
    """Build a complete signal record from a manually-supplied URL and/or text.

    Used when a salesperson finds something themselves (most commonly a
    LinkedIn post they spotted while logged in) and wants to draft an outreach
    sequence from it.

    Behaviour:
    - If `pasted_text` is provided and non-empty, we use it as the content
      source and don't attempt to fetch the URL. This is the LinkedIn path —
      LinkedIn typically blocks anonymous fetches, so the salesperson copies
      the post body in directly.
    - Otherwise we try to fetch the URL via the same fetcher used by the
      enrichment pass. If that fails, we raise — the UI should prompt the
      user to paste the text manually.
    - If `augment_with_apollo` is True, also queries Apollo for candidate
      contacts at the company. Stored as `apollo_contacts` separate from
      `named_individuals` (which come from the article itself).

    Returns a dict in the same shape as a Gemini-discovered signal *plus* the
    enriched fields (operational_problem, named_individuals, etc.), so it can
    flow into generate_sequence() with no special-casing downstream.

    May return {"error": "..."} if Claude judges the content doesn't contain
    a usable signal — caller should handle this and surface the message.
    """
    if not url or not url.strip():
        raise ValueError("URL is required.")

    url = url.strip()
    content: Optional[str] = (pasted_text or "").strip() or None

    if content is None:
        content = fetch_article(url)
        if content is None:
            raise RuntimeError(
                "Couldn't fetch that URL (login wall, paywall, 404, or "
                "JS-only page). Paste the post body or article text into "
                "the text field below and try again."
            )

    if len(content) < 50:
        raise RuntimeError(
            "Content is too short to extract a signal from. Paste at least "
            "a few sentences of post body or article text."
        )

    prompt = MANUAL_SIGNAL_PROMPT.format(
        vertical=vertical,
        url=url,
        content=content[:MAX_ARTICLE_CHARS],
    )

    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1500,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )

    text = _strip_json(response.content[0].text or "")
    try:
        signal = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude returned non-JSON output. First 400 chars:\n{text[:400]}"
        ) from e

    # Pass through Claude's "this isn't a usable signal" verdict to the caller.
    if isinstance(signal, dict) and "error" in signal and "company" not in signal:
        return signal

    # Make sure source_url is set (Claude should set it from the template, but
    # belt-and-braces in case the model dropped it).
    signal.setdefault("source_url", url)
    signal["fetch_status"] = "ok"
    signal["fetch_note"] = "Built from manual input."
    signal["apollo_contacts"] = _safe_apollo_lookup(
        signal.get("company", ""),
        vertical if augment_with_apollo else None,
    )
    return signal
