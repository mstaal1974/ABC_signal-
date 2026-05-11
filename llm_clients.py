"""LLM client wrappers.

- Gemini 2.5 Flash is used with Google Search grounding to find public hiring
  signals. Google Search grounding is the official way to give Gemini live
  web access; it does not involve scraping LinkedIn or any other site
  directly.
- Claude Sonnet 4.6 drafts the 3-part outreach sequence and, on a re-pass,
  rewrites anything the Compliance Firewall flags.

SDK note: This uses the unified `google-genai` SDK (`from google import genai`).
The older `google-generativeai` package is deprecated. If you're upgrading from
the old code, `pip uninstall google-generativeai && pip install google-genai`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from google import genai
from google.genai.types import GenerateContentConfig, GoogleSearch, Tool

from prompts import (
    CLAUDE_REWRITE_PROMPT,
    CLAUDE_SEQUENCE_PROMPT,
    GEMINI_SIGNAL_PROMPT,
    VERTICAL_CONTEXT,
)

# Default models — swap freely.
# Gemini options (May 2026):
#   - "gemini-2.5-flash"  — stable workhorse, supports Google Search grounding
#   - "gemini-3-flash"    — newer, frontier-class at workhorse cost
#   - "gemini-3-pro"      — flagship, best for hardest signal-extraction tasks
# Claude options:
#   - "claude-sonnet-4-6" — workhorse for outreach copy
#   - "claude-opus-4-7"   — premium quality, higher cost
GEMINI_MODEL = "gemini-2.5-flash"
CLAUDE_MODEL = "claude-sonnet-4-6"


# Module-level Gemini client (set by configure()).
_gemini_client: Optional[genai.Client] = None


def configure(gemini_key: str, anthropic_key: str) -> anthropic.Anthropic:
    """Configure both SDKs. Returns the Anthropic client; the Gemini client
    is held at module level so fetch_signals() can use it without being
    passed around."""
    global _gemini_client
    _gemini_client = genai.Client(api_key=gemini_key)
    return anthropic.Anthropic(api_key=anthropic_key)


# --- JSON extraction helpers ---

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json(text: str) -> str:
    """Strip optional markdown fences and surrounding whitespace from JSON text."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    # Grounded Gemini responses sometimes prepend a narrative sentence even
    # when asked for strict JSON. Fall back to first '{' .. last '}'.
    if not text.startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]
    return text


# --- Gemini: signal scouting ---


def fetch_signals(
    vertical: str,
    state: str,
    max_signals: int = 8,
) -> List[Dict[str, Any]]:
    """Use Gemini with Google Search grounding to find intent signals."""
    if _gemini_client is None:
        raise RuntimeError(
            "Gemini client not configured. Call llm_clients.configure() first."
        )
    if vertical not in VERTICAL_CONTEXT:
        raise ValueError(f"Unknown vertical: {vertical}")

    ctx = VERTICAL_CONTEXT[vertical]
    prompt = GEMINI_SIGNAL_PROMPT.format(
        vertical=vertical,
        state=state,
        signal_types="\n".join(f"- {s}" for s in ctx["signal_types"]),
        max_signals=max_signals,
    )

    try:
        response = _gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                tools=[Tool(google_search=GoogleSearch())],
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Gemini call failed (model={GEMINI_MODEL}). Verify the model "
            f"name and API key. Error: {e}"
        ) from e

    text = _strip_json(response.text or "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Gemini returned non-JSON output. First 400 chars:\n{text[:400]}"
        ) from e

    signals = data.get("signals", [])
    if not isinstance(signals, list):
        return []
    return signals[:max_signals]


# --- Claude: sequence generation and compliance rewrite ---


def generate_sequence(
    client: anthropic.Anthropic,
    signal: Dict[str, Any],
    vertical: str,
    model: str = CLAUDE_MODEL,
) -> Dict[str, Any]:
    """Use Claude to generate the 3-part outreach sequence from a signal."""
    if vertical not in VERTICAL_CONTEXT:
        raise ValueError(f"Unknown vertical: {vertical}")

    ctx = VERTICAL_CONTEXT[vertical]
    prompt = CLAUDE_SEQUENCE_PROMPT.format(
        vertical=vertical,
        priority_courses="\n".join(f"- {c}" for c in ctx["priority_courses"]),
        signal_json=json.dumps(signal, indent=2),
    )

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0.4,
        messages=[{"role": "user", "content": prompt}],
    )

    text = _strip_json(response.content[0].text or "")
    return json.loads(text)


def rewrite_for_compliance(
    client: anthropic.Anthropic,
    sequence: Dict[str, Any],
    violations: List[Tuple[str, str]],
    model: str = CLAUDE_MODEL,
) -> Dict[str, Any]:
    """Ask Claude to rewrite a sequence that failed the Compliance Firewall."""
    violation_summary = "\n".join(
        f'- "{term}" — {reason}' for term, reason in violations
    )
    prompt = CLAUDE_REWRITE_PROMPT.format(
        violations=violation_summary,
        sequence_json=json.dumps(sequence, indent=2),
    )

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )

    text = _strip_json(response.content[0].text or "")
    return json.loads(text)
