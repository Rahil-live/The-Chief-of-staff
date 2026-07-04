"""
approval_gate.py
================

Streamlit-based "Human-in-the-Loop" approval gate for the AI email ghostwriter.

This is the safety layer that sits between the AI draft and the real world.
No email is ever sent (or even queued to send) without an explicit human
APPROVE click. The human can also EDIT the draft or REJECT it and ask for a
regeneration.

Architecture
------------
    [Email thread] -> context_builder (prompts)
                  -> draft_machine   (Gemini draft)
                  -> approval_gate   <-- YOU ARE HERE (human gate)
                  -> approved_drafts.json  (only after APPROVE)

Run with:
    streamlit run approval_gate.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Environment / .env loading  (must run before importing draft_machine)
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_HERE / ".env")
load_dotenv()  # also try CWD

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import streamlit as st

# Local project imports. These are in the same directory as this file.
# Add the script's directory to sys.path so `import context_builder` and
# `import draft_machine` work no matter how Streamlit is launched.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import context_builder  # noqa: E402
import draft_machine    # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APPROVED_DRAFTS_PATH = _HERE / "approved_drafts.json"

# Three sample email threads for the dropdown. They cover a mix of common
# real-world situations: a cross-functional ask, a customer bug, and a
# launch-date check-in.
SAMPLE_THREADS: dict[str, dict[str, Any]] = {
    "Q3 Roadmap Review - need your input by Friday": {
        "subject": "Q3 Roadmap Review - need your input by Friday",
        "messages": [
            {
                "from": "Elena Park <elena.park@acme.com>",
                "date": "2026-06-12 10:42",
                "body": (
                    "Hi Rahul,\n\n"
                    "Hope your week's going well. I'm putting together the Q3 review "
                    "deck and would love a short paragraph from you on the Onboarding "
                    "rewrite. Specifically: status, biggest risk, and what you need "
                    "from leadership to land it.\n\n"
                    "Could you send something by EOD Friday? Even 4-5 lines is fine.\n\n"
                    "Thanks!\nElena"
                ),
            },
        ],
    },
    "Dashboard keeps timing out": {
        "subject": "Dashboard keeps timing out",
        "messages": [
            {
                "from": "Marcus Lee <marcus.lee@customer.example.com>",
                "date": "2026-06-13 14:08",
                "body": (
                    "Hi support team,\n\n"
                    "The analytics dashboard has been timing out for me every time I "
                    "try to load the 'Last 30 days' view. It's been happening for the "
                    "last two days. I have an exec review tomorrow morning and really "
                    "need this working.\n\n"
                    "Can someone take a look?\n\n"
                    "Thanks,\nMarcus"
                ),
            },
        ],
    },
    "Nov launch - is Nov 12 realistic?": {
        "subject": "Nov launch - is Nov 12 realistic?",
        "messages": [
            {
                "from": "Sam Rivera <sam.rivera@acme.com>",
                "date": "2026-06-13 17:55",
                "body": (
                    "Hey Rahul,\n\n"
                    "Marketing is asking if we can hold Nov 12 for the v2 launch. "
                    "Honest read on whether that's realistic, or whether we should "
                    "pull the date? They'd rather know now than scramble later.\n\n"
                    "Thanks,\nSam"
                ),
            },
        ],
    },
}

THREAD_OPTIONS = ["-- Select a sample thread --"] + list(SAMPLE_THREADS.keys())


# ---------------------------------------------------------------------------
# Streamlit page config (must be the first Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Approval Gate - AI Email Ghostwriter",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Custom CSS - dark theme + status colors
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<style>
    /* ---- Base dark theme tweaks ---- */
    .stApp {
        background-color: #0e1117;
    }
    section[data-testid="stSidebar"] {
        background-color: #161b22;
    }

    /* ---- Thread message boxes ---- */
    .thread-message {
        background-color: #1c2128;
        border: 1px solid #30363d;
        border-left: 4px solid #58a6ff;
        border-radius: 6px;
        padding: 12px 16px;
        margin: 10px 0;
        color: #e6edf3;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }
    .thread-message .msg-meta {
        color: #8b949e;
        font-size: 0.85em;
        margin-bottom: 6px;
    }
    .thread-message .msg-meta .sender {
        color: #58a6ff;
        font-weight: 600;
    }
    .thread-message .msg-body {
        white-space: pre-wrap;
        line-height: 1.5;
    }
    .thread-subject {
        background-color: #21262d;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 10px 14px;
        margin-bottom: 12px;
        color: #f0f6fc;
        font-weight: 600;
    }

    /* ---- Draft display ---- */
    .draft-box {
        background-color: #0d1117;
        border: 1px solid #30363d;
        border-left: 4px solid #d29922;
        border-radius: 6px;
        padding: 16px 20px;
        margin: 10px 0;
        color: #e6edf3;
        white-space: pre-wrap;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        line-height: 1.55;
    }
    .draft-meta {
        color: #8b949e;
        font-size: 0.85em;
        margin-bottom: 10px;
    }
    .draft-meta .label {
        color: #d29922;
        font-weight: 600;
    }

    /* ---- Status banners ---- */
    .status-approved {
        background-color: #033a16;
        border: 1px solid #2ea043;
        border-left: 6px solid #2ea043;
        border-radius: 6px;
        padding: 12px 18px;
        color: #aff5b4;
        font-weight: 600;
        margin: 10px 0;
    }
    .status-rejected {
        background-color: #3a0d0d;
        border: 1px solid #f85149;
        border-left: 6px solid #f85149;
        border-radius: 6px;
        padding: 12px 18px;
        color: #ffb4b0;
        font-weight: 600;
        margin: 10px 0;
    }
    .status-editing {
        background-color: #3a2a05;
        border: 1px solid #d29922;
        border-left: 6px solid #d29922;
        border-radius: 6px;
        padding: 12px 18px;
        color: #f0c674;
        font-weight: 600;
        margin: 10px 0;
    }
    .status-info {
        background-color: #0c2d6b;
        border: 1px solid #1f6feb;
        border-left: 6px solid #1f6feb;
        border-radius: 6px;
        padding: 12px 18px;
        color: #b6d6ff;
        margin: 10px 0;
    }

    /* ---- Header ---- */
    .gate-header {
        background: linear-gradient(90deg, #1f6feb 0%, #d29922 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        font-size: 1.8em;
        font-weight: 700;
        margin-bottom: 4px;
    }
    .gate-sub {
        color: #8b949e;
        font-size: 0.95em;
        margin-bottom: 18px;
    }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session-state initialization
# ---------------------------------------------------------------------------

def _init_state() -> None:
    """Initialize all session_state keys the app relies on."""
    defaults: dict[str, Any] = {
        "selected_thread": None,          # the thread currently picked
        "draft_thread": None,             # the thread that produced current_draft
        "current_draft": None,            # the latest generated draft body
        "draft_meta": None,               # metadata from draft_machine
        "status": "none",                 # none | approved | editing | rejected
        "edit_buffer": "",                # working text in the EDIT text area
        "generation_count": 0,            # how many drafts generated this session
        "api_key_override": None,         # user-entered key (overrides env)
        "last_error": None,               # last error from a failed generation
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


_init_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def render_thread_html(thread: dict[str, Any]) -> str:
    """Build the HTML for a thread (subject + each message as a styled box)."""
    subject = thread.get("subject", "(no subject)")
    messages = thread.get("messages", []) or []

    parts: list[str] = []
    parts.append(f'<div class="thread-subject">📧 {subject}</div>')
    for msg in messages:
        sender = msg.get("from", "(unknown sender)")
        date = msg.get("date", "(unknown date)")
        body = (msg.get("body") or "").strip()
        parts.append(
            f'<div class="thread-message">'
            f'  <div class="msg-meta">'
            f'    <span class="sender">{sender}</span> &middot; {date}'
            f'  </div>'
            f'  <div class="msg-body">{body}</div>'
            f'</div>'
        )
    return "\n".join(parts)


def render_draft_html(draft: str, meta: dict[str, Any] | None = None) -> str:
    """Build the HTML for the draft display box."""
    parts: list[str] = []
    if meta:
        parts.append(
            f'<div class="draft-meta">'
            f'  <span class="label">Model:</span> {meta.get("model", "?")} '
            f'&middot; '
            f'  <span class="label">To:</span> {meta.get("reply_to", "?")} '
            f'&middot; '
            f'  <span class="label">Chars:</span> {meta.get("char_count", len(draft))}'
            f'</div>'
        )
    parts.append(f'<div class="draft-box">{draft}</div>')
    return "\n".join(parts)


def save_approved_draft(
    draft: str,
    thread: dict[str, Any] | None,
    meta: dict[str, Any] | None = None,
    source: str = "ai",
) -> None:
    """Append an approved draft to approved_drafts.json (creates file if needed).

    On-disk format: a JSON list of objects with timestamp, source,
    thread_subject, reply_to, model, char_count, and draft body.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source": source,
        "thread_subject": (thread or {}).get("subject", ""),
        "reply_to": (meta or {}).get("reply_to", ""),
        "model": (meta or {}).get("model", ""),
        "char_count": len(draft),
        "draft": draft,
    }

    if APPROVED_DRAFTS_PATH.exists():
        try:
            with APPROVED_DRAFTS_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        except (json.JSONDecodeError, OSError):
            data = []
    else:
        data = []

    data.append(record)

    with APPROVED_DRAFTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def try_parse_custom_thread(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a JSON string pasted by the user as a thread.

    Returns (thread, None) on success or (None, error_message) on failure.
    """
    if not raw.strip():
        return None, "Paste a JSON thread first."
    try:
        thread = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"
    if not isinstance(thread, dict):
        return None, "Thread must be a JSON object."
    if "subject" not in thread or "messages" not in thread:
        return None, "Thread must contain 'subject' and 'messages' keys."
    if not isinstance(thread["messages"], list) or not thread["messages"]:
        return None, "'messages' must be a non-empty list."
    return thread, None


def resolve_api_key() -> str | None:
    """Return the effective Gemini API key (override > env), or None."""
    override = st.session_state.get("api_key_override")
    if override:
        return override
    return os.getenv("GEMINI_API_KEY")


def reset_draft_state() -> None:
    """Clear the draft-related session state."""
    st.session_state.current_draft = None
    st.session_state.draft_meta = None
    st.session_state.draft_thread = None
    st.session_state.edit_buffer = ""
    st.session_state.status = "none"
    st.session_state.last_error = None


# ---------------------------------------------------------------------------
# Sidebar - thread selection + API key
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🛡️ Approval Gate")
    st.markdown("Human-in-the-loop review for the AI email ghostwriter.")
    st.markdown("---")

    st.markdown("#### 📥 Email thread")

    choice = st.selectbox(
        "Sample thread",
        options=THREAD_OPTIONS,
        index=0,
        key="sample_choice",
    )

    st.markdown("**Or paste your own thread JSON:**")
    custom_json = st.text_area(
        "Custom thread JSON",
        value="",
        height=180,
        placeholder='{"subject": "...", "messages": [{"from": "...", "date": "...", "body": "..."}]}',
        label_visibility="collapsed",
    )

    use_custom = st.checkbox("Use custom thread", value=False)

    # Resolve the selected thread (custom takes precedence if checked).
    selected_thread: dict[str, Any] | None = None
    if use_custom:
        parsed, err = try_parse_custom_thread(custom_json)
        if err:
            st.error(f"❌ {err}")
        else:
            selected_thread = parsed
            st.success(f"✅ Loaded custom thread: {parsed.get('subject', '(no subject)')}")
    elif choice and choice != THREAD_OPTIONS[0]:
        selected_thread = SAMPLE_THREADS[choice]

    st.session_state.selected_thread = selected_thread

    st.markdown("---")
    st.markdown("#### 🔑 API key")

    env_key_present = bool(os.getenv("GEMINI_API_KEY"))
    if env_key_present:
        st.success("✅ GEMINI_API_KEY found in environment")
    else:
        st.warning("⚠️ GEMINI_API_KEY not set - paste it below or add to .env")

    api_key_input = st.text_input(
        "Gemini API key (override)",
        value=st.session_state.get("api_key_override") or "",
        type="password",
        help="Leave blank to use the value from .env / environment.",
    )
    if api_key_input:
        st.session_state.api_key_override = api_key_input
    else:
        st.session_state.api_key_override = None

    effective_key = resolve_api_key()
    if not effective_key:
        st.error("❌ No API key available. Add one to .env or paste it above.")

    st.markdown("---")
    st.markdown("#### 🔁 Generate")
    generate_clicked = st.button(
        "✨ Generate Draft",
        type="primary",
        use_container_width=True,
        disabled=(selected_thread is None) or (effective_key is None),
    )

    st.markdown("---")
    st.markdown("#### 📊 Session stats")
    st.markdown(f"- Drafts generated: **{st.session_state.generation_count}**")
    st.markdown(f"- Current status: **{st.session_state.status}**")

    if st.button("🔄 Reset session", use_container_width=True):
        st.session_state.selected_thread = None
        st.session_state.draft_thread = None
        st.session_state.current_draft = None
        st.session_state.draft_meta = None
        st.session_state.status = "none"
        st.session_state.edit_buffer = ""
        st.session_state.generation_count = 0
        st.session_state.last_error = None
        st.success("Session reset.")


# ---------------------------------------------------------------------------
# Generation handler (runs when the Generate button is clicked)
# ---------------------------------------------------------------------------

if generate_clicked and selected_thread is not None and effective_key is not None:
    # If a key override was set in the UI, push it into the environment so
    # draft_machine picks it up (it reads GEMINI_API_KEY via os.getenv).
    if st.session_state.get("api_key_override"):
        os.environ["GEMINI_API_KEY"] = st.session_state.api_key_override

    with st.spinner("🤖 Generating draft with Gemini..."):
        try:
            result = draft_machine.draft_reply_with_metadata(selected_thread)
            st.session_state.current_draft = result["draft"]
            st.session_state.draft_meta = result
            st.session_state.draft_thread = selected_thread
            st.session_state.status = "none"
            st.session_state.edit_buffer = result["draft"]
            st.session_state.generation_count += 1
            st.session_state.last_error = None
        except Exception as e:  # noqa: BLE001 - we want to surface any error
            st.session_state.last_error = str(e)
            st.session_state.current_draft = None
            st.session_state.draft_meta = None
            st.session_state.draft_thread = None
            st.session_state.status = "none"


# ---------------------------------------------------------------------------
# Main area - header
# ---------------------------------------------------------------------------

st.markdown(
    '<div class="gate-header">🛡️ Human-in-the-Loop Approval Gate</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="gate-sub">'
    'Review the AI-generated draft below, then <b>APPROVE</b>, <b>EDIT</b>, or '
    '<b>REJECT</b> it. Nothing is sent without your explicit approval.'
    '</div>',
    unsafe_allow_html=True,
)

if st.session_state.last_error:
    st.markdown(
        f'<div class="status-rejected">❌ Generation failed: '
        f'{st.session_state.last_error}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Two-column layout: thread (left) | draft (right)
# ---------------------------------------------------------------------------

# Pick which thread to show. Prefer the thread that produced the current
# draft; fall back to the user's current selection.
display_thread = st.session_state.draft_thread or st.session_state.selected_thread

_cols = st.columns([1, 1], gap="large")
left_col, right_col = _cols[0], _cols[1]

with left_col:
    st.markdown("#### 📥 Email thread")
    if display_thread:
        st.markdown(render_thread_html(display_thread), unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="status-info">👈 Pick a thread in the sidebar '
            '(sample or paste your own JSON) and click <b>Generate Draft</b>.</div>',
            unsafe_allow_html=True,
        )

with right_col:
    st.markdown("#### ✍️ AI draft reply")

    draft = st.session_state.current_draft
    meta = st.session_state.draft_meta
    status = st.session_state.status

    if not draft:
        st.markdown(
            '<div class="status-info">No draft yet. Pick a thread and click '
            '<b>Generate Draft</b> in the sidebar.</div>',
            unsafe_allow_html=True,
        )
    else:
        # Show the draft text (read-only display).
        st.markdown(render_draft_html(draft, meta), unsafe_allow_html=True)

        # ---------------- Status banner ----------------
        if status == "approved":
            st.markdown(
                '<div class="status-approved">✅ APPROVED &middot; This draft is '
                'ready to send. Saved to approved_drafts.json.</div>',
                unsafe_allow_html=True,
            )
        elif status == "rejected":
            st.markdown(
                '<div class="status-rejected">❌ REJECTED &middot; This draft was '
                'discarded. Click <b>Generate Draft</b> in the sidebar to try again.'
                '</div>',
                unsafe_allow_html=True,
            )
        elif status == "editing":
            st.markdown(
                '<div class="status-editing">✏️ EDITING &middot; Modify the draft '
                'below, then click <b>Approve edited version</b>.</div>',
                unsafe_allow_html=True,
            )

        # ---------------- Action buttons ----------------
        st.markdown("---")
        st.markdown("##### Actions")

        if status != "editing":
            # Read-only mode: show the three primary actions.
            btn_cols = st.columns([1, 1, 1])
            with btn_cols[0]:
                approve_clicked = st.button(
                    "✅ APPROVE",
                    type="primary",
                    use_container_width=True,
                    disabled=(status == "approved"),
                )
            with btn_cols[1]:
                edit_clicked = st.button(
                    "✏️ EDIT",
                    use_container_width=True,
                )
            with btn_cols[2]:
                reject_clicked = st.button(
                    "❌ REJECT",
                    use_container_width=True,
                )

            # ---- Handle APPROVE ----
            if approve_clicked and status != "approved":
                try:
                    save_approved_draft(
                        draft=draft,
                        thread=st.session_state.draft_thread,
                        meta=meta,
                        source="ai",
                    )
                    st.session_state.status = "approved"
                    st.session_state.edit_buffer = draft
                    st.success(
                        f"✅ Approved & saved to {APPROVED_DRAFTS_PATH.name}"
                    )
                except Exception as e:  # noqa: BLE001
                    st.error(f"❌ Failed to save approved draft: {e}")

            # ---- Handle EDIT ----
            if edit_clicked:
                st.session_state.status = "editing"
                # Make sure the text area starts with the latest draft body.
                st.session_state.edit_buffer = draft

            # ---- Handle REJECT ----
            if reject_clicked:
                st.session_state.status = "rejected"
                st.warning("Draft rejected. Regenerate to try again.")

        else:
            # Editing mode: show the text area + approve-edited button.
            edited_text = st.text_area(
                "Edit the draft",
                value=st.session_state.edit_buffer or draft,
                height=320,
                key="edit_text_area",
            )
            # Keep the buffer in sync so it persists across reruns.
            st.session_state.edit_buffer = edited_text

            edit_btn_cols = st.columns([1, 1, 1])
            with edit_btn_cols[0]:
                approve_edited_clicked = st.button(
                    "✅ Approve edited version",
                    type="primary",
                    use_container_width=True,
                )
            with edit_btn_cols[1]:
                revert_clicked = st.button(
                    "↩️ Revert to AI draft",
                    use_container_width=True,
                )
            with edit_btn_cols[2]:
                cancel_clicked = st.button(
                    "✖️ Cancel edit",
                    use_container_width=True,
                )

            if approve_edited_clicked:
                final_text = (edited_text or "").strip()
                if not final_text:
                    st.error("Edited draft is empty - add some text first.")
                else:
                    try:
                        save_approved_draft(
                            draft=final_text,
                            thread=st.session_state.draft_thread,
                            meta=meta,
                            source="edited",
                        )
                        # Update the displayed draft to the edited version
                        # and mark as approved.
                        st.session_state.current_draft = final_text
                        if st.session_state.draft_meta:
                            st.session_state.draft_meta["char_count"] = len(final_text)
                        st.session_state.status = "approved"
                        st.success(
                            f"✅ Edited version approved & saved to "
                            f"{APPROVED_DRAFTS_PATH.name}"
                        )
                    except Exception as e:  # noqa: BLE001
                        st.error(f"❌ Failed to save edited draft: {e}")

            if revert_clicked:
                st.session_state.edit_buffer = draft
                st.info("Reverted to the original AI draft text.")

            if cancel_clicked:
                st.session_state.status = "none"
                st.session_state.edit_buffer = draft
                st.info("Edit cancelled. Draft returned to read-only view.")


# ---------------------------------------------------------------------------
# Footer - quick help
# ---------------------------------------------------------------------------

st.markdown("---")
with st.expander("ℹ️ How this gate works", expanded=False):
    st.markdown(
        """
**The safety contract**

1. The AI (`draft_machine.py` + Gemini) generates a draft reply.
2. **Nothing is ever sent automatically.** You must explicitly click `APPROVE`.
3. You can `EDIT` the draft first - your edited version (not the AI's) is
   what gets saved.
4. You can `REJECT` a draft and ask for a new one - rejected drafts are
   discarded (not saved).
5. `APPROVE` writes the draft to `approved_drafts.json` with a timestamp.
   That file is the queue your downstream "send" step (if any) should
   consume from - and it should still be a *human-triggered* step.

**Session state**

- `current_draft`     - the text on screen
- `draft_meta`        - model / recipient / length metadata
- `status`            - `none` | `approved` | `editing` | `rejected`
- `generation_count`  - how many drafts this session has produced
- `edit_buffer`       - working text inside the EDIT text area

**Run**

```bash
streamlit run approval_gate.py
```
        """
    )
