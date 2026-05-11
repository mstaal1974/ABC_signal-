# ABC Training — Intent Signals Console

A Streamlit app that converts public hiring and contract-win signals into compliant 3-part outreach sequences for ABC Training (RTO #5800).

## What it does

1. **Sidebar** with four verticals — Laboratory, CMT, Pathology, Manufacturing — plus a state filter.
2. **Scan tab.** Gemini 2.5 Flash (with Google Search grounding) scouts public news, ASX announcements, government tender awards, and company press releases for hiring or growth signals in the chosen vertical and state.
3. **Generate tab.** Pick a signal. Claude Sonnet 4.6 drafts a Skills-First 3-part outreach sequence (LinkedIn message, Email, Phone script). High-urgency signals get the **Unbundled Microcredentials** angle automatically — get the team operationally compliant in days/weeks via unit-of-competency delivery rather than waiting months for a full qualification.
4. **Compliance Firewall** scans every Claude output for banned terms ("guaranteed jobs", "cheapest", "free training", outcome guarantees, unsubstantiated superlatives). On violation, the app auto-asks Claude to rewrite up to twice. Anything still flagged is **blocked** from clean display until a human edits it.

## Setup

```bash
# 1. Install
pip install -r requirements.txt

# 2. Create your secrets file
cp .streamlit/secrets.toml.example .streamlit/secrets.toml

# 3. Generate a bcrypt hash for each user
python -c "import auth; print(auth.generate_hash('your-password'))"
# Paste the resulting hash into [users] in secrets.toml

# 4. Add your API keys to [api_keys] in secrets.toml
#    - Gemini:    https://aistudio.google.com/apikey
#    - Anthropic: https://console.anthropic.com

# 5. Run
streamlit run app.py
```

### Upgrading from an earlier version

If you previously installed `google-generativeai`, uninstall it before installing the new SDK. The two packages share a `google` namespace and will collide:

```bash
pip uninstall google-generativeai
pip install -r requirements.txt
```

The current code uses the unified `google-genai` SDK (`from google import genai`). The older `google-generativeai` package is deprecated by Google.

## Important caveats — read before deploying

- **LinkedIn scraping is against LinkedIn's Terms of Service.** It will get accounts and IP ranges banned, and exposes you to legal risk in Australia under the Copyright Act and the CFAA-equivalent provisions. This app uses Gemini's grounded Google Search to find signals from compliant public sources instead. If you need actual LinkedIn data, use **LinkedIn Sales Navigator API** or a licensed data provider (Cognism, Apollo, Lusha). I can add a Sales Navigator adapter if you have a licence.
- **The Compliance Firewall is a pattern-based safety net, not a legal review.** Final responsibility for ASQA and Australian Consumer Law compliance sits with your RTO's compliance officer. Always have a human review marketing copy before bulk send. The regex patterns in `compliance.py` are a starting set — extend them as your compliance team identifies new phrases to block.
- **Authentication is bcrypt + session state.** Fine for a small internal tool behind your VPN or on Streamlit Cloud. For broader deployment use SSO (Okta, Azure AD, Google Workspace) via a reverse proxy.
- **Claude Sonnet 4.6** (`claude-sonnet-4-6`) is wired in. Claude Opus 4.7 produces marginally better outreach copy at higher cost — drop-in compatible, change `CLAUDE_MODEL` in `llm_clients.py`.
- **Gemini 2.5 Flash** (`gemini-2.5-flash`) is the default — stable workhorse with Google Search grounding. Gemini 3 Flash and Gemini 3 Pro are also available; change `GEMINI_MODEL` in `llm_clients.py`.
- **Gemini's Google Search tool** sometimes refuses to return JSON when the search returns nothing relevant. The app handles this gracefully by showing an empty result.

## Customising for your scope of registration

The `prompts.py` file is the main config surface:

- **`VERTICAL_CONTEXT`** — replace the example course codes with your actual scope of registration from training.gov.au. Each vertical has `signal_types` (what Gemini looks for) and `priority_courses` (what Claude maps signals to). Put your high-margin and high-volume qualifications first.
- **`GEMINI_SIGNAL_PROMPT`** — adjust the source preferences (ASX, AusTender, etc.) if you want to bias toward different industries.
- **`CLAUDE_SEQUENCE_PROMPT`** — this is where the Sales Playbook tone rules live. Edit the "CORE CONSTRAINTS" block to reflect your house style.
- **`compliance.py`** — extend `BANNED_PATTERNS` (hard blocks) and `WARNING_PATTERNS` (soft flags) as your compliance team identifies new phrases.

## File overview

```
abc_training_app/
├── app.py                       # Streamlit UI + orchestration
├── auth.py                      # bcrypt login + session
├── compliance.py                # Regex Compliance Firewall
├── llm_clients.py               # Gemini + Claude API wrappers
├── prompts.py                   # System prompts + vertical config
├── requirements.txt
├── README.md
└── .streamlit/
    └── secrets.toml.example     # Config template
```

## Compliance Firewall — what it blocks

Hard blocks (auto-rewrite, then block if still present):

- "guaranteed job(s) / employment / placement / outcome"
- "100% job placement", "100% pass rate" and similar absolute outcome claims
- "cheapest", "lowest price/cost/fee" — comparative pricing
- "free training", "no-cost course/training/qualification"
- "ASQA approved/endorsed/recommended"

Soft warnings (surface to user, do not block):

- "best RTO/training provider"
- "#1 RTO/provider/training"
- "nationally recognised" — flagged because the named course must actually be on your scope
- "guaranteed salary/wage/pay"
- "pass rate of N..." — specific stats need evidence

All matching is case-insensitive with word boundaries.
