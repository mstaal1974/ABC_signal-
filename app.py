"""ABC Training — Intent Signals Console.

Streamlit app that:
1. Authenticates the user.
2. Scans for hiring/contract intent signals using Gemini 2.5 Flash with Google
   Search grounding, scoped by vertical (Laboratory / CMT / Pathology /
   Manufacturing) and state.
3. Enriches each signal by fetching the source article and running Claude
   Haiku 4.5 over it to extract operational detail (contract value, project
   duration, named individuals, the specific operational problem, skills
   implied). Runs in parallel.
4. Generates Skills-First 3-part outreach sequences (LinkedIn / Email /
   Phone) using Claude Sonnet 4.6, with the enriched fields fed in.
5. Runs every Claude output through a regex Compliance Firewall, with up to
   two auto-rewrite passes before either clearing or blocking the draft.

Run with:
    streamlit run app.py
"""
from __future__ import annotations

import json

import streamlit as st

import auth
import compliance
import enrichment
import llm_clients
from prompts import VERTICAL_CONTEXT

# ----------------------------------------------------------------------
# Page config
# ----------------------------------------------------------------------
st.set_page_config(
    page_title="ABC Training | Intent Signals",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ----------------------------------------------------------------------
# Auth gate
# ----------------------------------------------------------------------
if not auth.login_form():
    st.stop()

# ----------------------------------------------------------------------
# Configure LLM clients from secrets
# ----------------------------------------------------------------------
try:
    gemini_key = st.secrets["api_keys"]["gemini"]
    anthropic_key = st.secrets["api_keys"]["anthropic"]
except (KeyError, FileNotFoundError):
    st.error(
        "Missing API keys. Create `.streamlit/secrets.toml` with `[api_keys]` "
        "entries for `gemini` and `anthropic`. See README.md."
    )
    st.stop()

claude_client = llm_clients.configure(gemini_key, anthropic_key)

# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"**Signed in:** `{st.session_state.username}`")
    auth.logout_button()
    st.divider()

    st.markdown("### Vertical")
    vertical = st.radio(
        "Vertical",
        list(VERTICAL_CONTEXT.keys()),
        label_visibility="collapsed",
        key="vertical",
    )

    state = st.selectbox(
        "State / Territory",
        ["QLD", "NSW", "VIC", "WA", "SA", "TAS", "ACT", "NT", "National"],
        key="state",
    )

    max_signals = st.slider("Max signals per scan", 3, 15, 8)

    auto_enrich = st.toggle(
        "Auto-enrich signals",
        value=True,
        help=(
            "After scanning, fetch each signal's source article and use "
            "Claude Haiku to extract contract value, named individuals, "
            "specific operational problem, and implied skills. Adds ~5 "
            "seconds per scan but produces far more actionable signals."
        ),
    )

    st.divider()
    st.caption("RTO #5800")
    st.caption("Compliance Firewall: **ENABLED**")
    st.caption("Scope: public web sources only (no LinkedIn scraping)")

# ----------------------------------------------------------------------
# Main panel
# ----------------------------------------------------------------------
st.title("🎯 Intent Signals Console")
st.caption(f"{vertical} · {state}")

tab_scan, tab_generate = st.tabs(["1 · Scan Signals", "2 · Generate Sequence"])


# --- Scan tab ---------------------------------------------------------
with tab_scan:
    col_info, col_btn = st.columns([3, 1])
    with col_info:
        st.markdown(
            "**Looking for:** "
            + "; ".join(VERTICAL_CONTEXT[vertical]["signal_types"])
        )
    with col_btn:
        scan_now = st.button(
            "🔍 Scan now", type="primary", use_container_width=True
        )

    if scan_now:
        with st.spinner(
            f"Scanning public sources for {vertical} signals in {state}..."
        ):
            try:
                signals = llm_clients.fetch_signals(vertical, state, max_signals)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Scan failed: {exc}")
                signals = []

        if signals and auto_enrich:
            with st.spinner(
                f"Enriching {len(signals)} signal(s) — fetching sources and "
                "extracting operational detail..."
            ):
                try:
                    signals = enrichment.enrich_signals(claude_client, signals)
                except Exception as exc:  # noqa: BLE001
                    st.warning(
                        f"Enrichment pass failed: {exc}. Showing headline-level "
                        "signals only."
                    )

        st.session_state.signals = signals
        # Reset any previously-selected signal so the user re-picks
        st.session_state.pop("selected_signal", None)
        st.session_state.pop("sequence", None)
        st.session_state.pop("compliance_result", None)

    signals = st.session_state.get("signals", [])

    if signals:
        # Enrichment summary line
        counts = enrichment.status_summary(signals)
        if counts.get("ok", 0) > 0 or "not_run" not in counts:
            ok = counts.get("ok", 0)
            failed = sum(v for k, v in counts.items() if k not in ("ok", "not_run"))
            if ok and failed:
                st.success(
                    f"Found {len(signals)} signal(s). Enriched {ok}; "
                    f"{failed} could not be enriched (paywall, blocked, or no URL)."
                )
            elif ok:
                st.success(
                    f"Found {len(signals)} signal(s). All enriched from source."
                )
            else:
                st.success(f"Found {len(signals)} signal(s).")
        else:
            st.success(f"Found {len(signals)} signal(s).")

        urgency_badge = {"high": "🔥", "medium": "🟡", "low": "⚪"}
        enrich_badge = {
            "ok": "✅",
            "fetch_failed": "⚠️",
            "extract_failed": "⚠️",
            "no_url": "⚪",
            "not_run": "",
            "unknown_error": "⚠️",
        }

        for i, sig in enumerate(signals):
            urgency = (sig.get("urgency") or "medium").lower()
            ubadge = urgency_badge.get(urgency, "⚪")
            ebadge = enrich_badge.get(sig.get("fetch_status", "not_run"), "")
            header = (
                f"{ubadge} {ebadge}  "
                f"{sig.get('company', 'Unknown')} — "
                f"{sig.get('headline', '(no headline)')}"
            )
            with st.expander(header):
                c1, c2 = st.columns([2, 1])
                with c1:
                    st.markdown(f"**Signal type:** {sig.get('signal_type', 'N/A')}")
                    st.markdown(f"**Snippet:** {sig.get('snippet', '')}")
                    if sig.get("source_url"):
                        st.markdown(f"[Source]({sig['source_url']})")
                with c2:
                    st.markdown(f"**Date:** {sig.get('date', 'unknown')}")
                    st.markdown(f"**Location:** {sig.get('location', 'unknown')}")
                    st.markdown(f"**Urgency:** `{urgency}`")
                    st.markdown(
                        f"**Team size hint:** {sig.get('team_size_hint', 'unknown')}"
                    )

                # --- Enriched detail block ---
                status = sig.get("fetch_status")
                if status == "ok":
                    st.markdown("---")
                    st.markdown("**Enriched detail** (source-verified)")
                    conf = sig.get("confidence", "low")
                    conf_color = {"high": "🟢", "medium": "🟡", "low": "🟠"}.get(
                        conf, "⚪"
                    )

                    op_problem = sig.get("operational_problem") or ""
                    if op_problem:
                        st.markdown(f"**Operational problem:** {op_problem}")

                    e1, e2 = st.columns(2)
                    with e1:
                        st.markdown(
                            f"**Contract value:** {sig.get('contract_value', 'unknown')}"
                        )
                        st.markdown(
                            f"**Project duration:** {sig.get('project_duration', 'unknown')}"
                        )
                        st.markdown(
                            f"**Geography:** {sig.get('geographic_footprint', 'unknown')}"
                        )
                        st.markdown(
                            f"**Team size (refined):** "
                            f"{sig.get('team_size_estimate', 'unknown')}"
                        )
                    with e2:
                        skills = sig.get("skills_implied") or []
                        if skills:
                            st.markdown("**Skills implied:**")
                            for s in skills:
                                st.markdown(f"- {s}")
                        else:
                            st.markdown("**Skills implied:** _(none extracted)_")

                    named = sig.get("named_individuals") or []
                    if named:
                        st.markdown("**Named individuals:**")
                        for person in named:
                            name = person.get("name", "")
                            role = person.get("role", "")
                            st.markdown(f"- {name} — *{role}*" if role else f"- {name}")

                    st.caption(f"{conf_color} Extraction confidence: `{conf}`")

                elif status in ("fetch_failed", "extract_failed", "no_url"):
                    st.markdown("---")
                    note = sig.get("fetch_note", "")
                    st.warning(
                        f"⚠️ Enrichment did not run: {note} "
                        "Using headline-level detail only."
                    )

                if st.button("Use this signal →", key=f"use_{i}"):
                    st.session_state.selected_signal = sig
                    st.session_state.selected_signal_idx = i
                    st.session_state.pop("sequence", None)
                    st.session_state.pop("compliance_result", None)
                    st.toast(
                        "Signal selected — open the Generate Sequence tab.",
                        icon="✅",
                    )
    elif "signals" in st.session_state:
        st.info(
            "No qualifying signals found. Try a different state, widen to "
            "'National', or adjust signal types in `prompts.py`."
        )
    else:
        st.info(
            "Press **Scan now** to look for recent hiring announcements, "
            "contract wins, and expansion signals in this vertical."
        )


# --- Generate tab -----------------------------------------------------
with tab_generate:
    signal = st.session_state.get("selected_signal")

    if not signal:
        st.info(
            "Select a signal from the **Scan Signals** tab first. "
            "The Compliance Firewall runs automatically on every draft."
        )
    else:
        st.markdown(
            f"### Signal\n**{signal.get('company')}** — *{signal.get('headline')}*"
        )

        if signal.get("fetch_status") == "ok" and signal.get("operational_problem"):
            st.info(
                f"**Operational problem (source-verified):** "
                f"{signal['operational_problem']}"
            )

        with st.expander("Full signal details (including enriched fields)"):
            st.json(signal)

        draft = st.button("✍️ Draft sequence", type="primary")

        if draft:
            with st.spinner("Drafting outreach sequence with Claude..."):
                try:
                    sequence = llm_clients.generate_sequence(
                        claude_client, signal, vertical
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Generation failed: {exc}")
                    sequence = None

            if sequence:
                # Compliance Firewall — up to 2 auto-rewrites
                result = compliance.scan_sequence(sequence)
                rewrite_count = 0
                while result.violations and rewrite_count < 2:
                    rewrite_count += 1
                    with st.spinner(
                        f"Compliance Firewall blocked — auto-rewriting "
                        f"(pass {rewrite_count})..."
                    ):
                        try:
                            sequence = llm_clients.rewrite_for_compliance(
                                claude_client, sequence, result.violations
                            )
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Rewrite failed: {exc}")
                            break
                    result = compliance.scan_sequence(sequence)

                st.session_state.sequence = sequence
                st.session_state.compliance_result = result
                st.session_state.rewrite_count = rewrite_count

        sequence = st.session_state.get("sequence")
        result = st.session_state.get("compliance_result")
        rewrite_count = st.session_state.get("rewrite_count", 0)

        if sequence and result:
            # --- Compliance banner ---
            if result.status == "CLEARED":
                st.success("✅ Compliance Firewall: CLEARED")
            elif result.status == "WARNING":
                st.warning(
                    f"⚠️ Compliance Firewall: {len(result.warnings)} "
                    "soft warning(s) — review before sending."
                )
            else:
                st.error(
                    f"🚫 Compliance Firewall: BLOCKED — "
                    f"{len(result.violations)} hard violation(s) remain "
                    "after auto-rewrite. Manual edit required before send."
                )

            if rewrite_count:
                st.caption(f"🔁 Auto-rewrites applied: {rewrite_count}")

            if result.violations:
                with st.expander("🚫 Hard violations", expanded=True):
                    for term, reason in result.violations:
                        st.markdown(f"- **`{term}`** — {reason}")
            if result.warnings:
                with st.expander(
                    f"⚠️ Soft warnings ({len(result.warnings)})"
                ):
                    for term, reason in result.warnings:
                        st.markdown(f"- **`{term}`** — {reason}")

            st.divider()

            # --- Mapping summary ---
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown("**Mapped course / units**")
                st.info(sequence.get("mapped_course", ""))
            with c2:
                st.markdown("**Angle**")
                angle = sequence.get("angle", "")
                if "Microcredential" in angle:
                    st.success(angle)
                else:
                    st.info(angle)
            with c3:
                st.markdown("**Signal read**")
                st.caption(sequence.get("signal_summary", ""))

            with st.expander("Rationale"):
                st.write(sequence.get("rationale", ""))

            st.divider()

            # --- The 3-part sequence ---
            st.markdown("### 1 · LinkedIn message")
            st.code(
                sequence.get("linkedin_message", ""),
                language=None,
                wrap_lines=True,
            )

            st.markdown("### 2 · Email")
            email = sequence.get("email", {}) or {}
            st.markdown(f"**Subject:** {email.get('subject', '')}")
            st.code(email.get("body", ""), language=None, wrap_lines=True)

            st.markdown("### 3 · Phone script")
            phone = sequence.get("phone_script", {}) or {}
            st.markdown(f"**Opener:** {phone.get('opener', '')}")
            st.markdown(f"**Value statement:** {phone.get('value_statement', '')}")
            st.markdown("**Discovery questions:**")
            for q in phone.get("discovery_questions", []) or []:
                st.markdown(f"- {q}")
            st.markdown(
                f"**Objection response:** {phone.get('objection_response', '')}"
            )

            st.divider()

            # --- Download ---
            fname = (
                f"{(signal.get('company') or 'sequence').replace(' ', '_')}_"
                f"{vertical.lower()}_sequence.json"
            )
            st.download_button(
                "⬇️ Download sequence as JSON",
                data=json.dumps(
                    {
                        "vertical": vertical,
                        "state": state,
                        "signal": signal,
                        "sequence": sequence,
                        "compliance": {
                            "status": result.status,
                            "violations": result.violations,
                            "warnings": result.warnings,
                            "auto_rewrites": rewrite_count,
                        },
                    },
                    indent=2,
                ),
                file_name=fname,
                mime="application/json",
            )
