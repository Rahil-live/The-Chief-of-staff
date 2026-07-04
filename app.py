"""
The Draft Desk — Streamlit UI for the Chief of Staff workflow.

Phases (driven by session_state.current_phase):
  1. Inbox & Triage
  2. Draft Generation
  3. Approval Gate
  4. Export Proof
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st
from task_logger import log_action, get_action_log  # type: ignore


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="The Draft Desk",
    page_icon="✍️",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SAMPLE_THREADS_PATH = BASE_DIR / "sample_threads.json"

PHASES = [
    "Inbox & Triage",
    "Draft Generation",
    "Approval Gate",
    "Export Proof",
]

# Triage buckets — keys match the strings triage.py emits, values are the
# display order and the emoji/header used in the UI.
PRIORITY_ORDER = ["urgent", "needs-reply", "fyi", "ignore"]
PRIORITY_META = {
    "urgent":      ("🚨 Urgent",      "Production incidents, blocking issues, deadlines today."),
    "needs-reply": ("💬 Needs Reply", "Active conversations waiting on your response."),
    "fyi":         ("📋 FYI",         "Informational only — no action required."),
    "ignore":      ("🗑️ Ignore",      "Newsletters, marketing, low-signal noise."),
}


# ---------------------------------------------------------------------------
# Lazy / safe imports of project modules
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _get_triage():
    """Import triage_inbox once; cache so we don't re-execute triage.py's
    module-level Gemini demo on every rerun."""
    from triage import triage_inbox  # type: ignore
    return triage_inbox


@st.cache_resource(show_spinner=False)
def _get_fetch_threads():
    """Import fetch_threads once."""
    from engine import fetch_threads  # type: ignore
    return fetch_threads


@st.cache_resource(show_spinner=False)
def _get_send_reply():
    """Import send_reply once."""
    from engine import send_reply  # type: ignore
    return send_reply


@st.cache_resource(show_spinner=False)
def _get_fetch_full_thread():
    """Import fetch_full_thread once."""
    from engine import fetch_full_thread  # type: ignore
    return fetch_full_thread


@st.cache_resource(show_spinner=False)
def _get_calendar_engine():
    """Import calendar_engine functions once."""
    from calendar_engine import (  # type: ignore
        parse_meeting_request,
        find_free_slot,
        create_event,
    )
    return parse_meeting_request, find_free_slot, create_event


# ---------------------------------------------------------------------------
# Session state initialization
# ---------------------------------------------------------------------------
def _init_session_state() -> None:
    """Initialize all session_state keys we rely on."""
    defaults: dict[str, Any] = {
        "threads": [],              # list[dict] — loaded email threads (UI format)
        "triaged": {},              # dict[str, list[dict]] — priority -> [thread, ...]
        "drafts": {},               # dict[str, str] — thread_id -> draft body
        "approved": {},             # dict[str, str] — thread_id -> approved draft
        "rejected": set(),          # set[str] — thread_ids that were rejected
        "sent": set(),              # set[str] — thread_ids that have been sent
        "booked": {},               # dict[str, dict] — thread_id -> Calendar event dict
        "current_phase": "Inbox & Triage",
        "source": "Sample threads", # "Sample threads" | "Gmail via engine.py"
        "last_pull_summary": None,  # str | None — message shown after a pull
        "pipeline_running": False,  # bool — True while _render_pipeline_execution runs
        "pipeline_log": [],         # list[str] — log from last pipeline run
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


_init_session_state()


# ---------------------------------------------------------------------------
# Source adapters — both paths return threads in the UI shape:
#   [{"id": str, "subject": str, "messages": [{"from", "date", "body"}, ...]}, ...]
# ---------------------------------------------------------------------------
def load_sample_threads() -> list[dict[str, Any]]:
    """Load sample threads from disk; return [] on any failure."""
    if not SAMPLE_THREADS_PATH.exists():
        return []
    try:
        with SAMPLE_THREADS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _preview_from_body(body: str, limit: int = 200) -> str:
    body = (body or "").strip().replace("\n", " ")
    return body if len(body) <= limit else body[: limit - 1] + "…"


def fetch_threads_via_engine() -> list[dict[str, Any]]:
    """
    Call engine.fetch_threads() and convert the result into the UI thread
    shape. If engine.py returns an MCP-plan dict instead of a thread list
    (because creds aren't configured), raise RuntimeError with a friendly
    message so the caller can surface it.
    """
    fetch_threads = _get_fetch_threads()
    result = fetch_threads()

    if not isinstance(result, list):
        # engine.py returned an MCP plan — we can't run that from Streamlit.
        raise RuntimeError(
            "engine.fetch_threads() returned an MCP plan instead of thread "
            "data. Gmail credentials (credentials.json / token.json) aren't "
            "configured for direct use, and the MCP path must be driven by "
            "an MCP-aware host. Run engine.py from a Cline session, or set "
            "up Gmail OAuth credentials."
        )

    converted: list[dict[str, Any]] = []
    for t in result:
        thread_id = t.get("thread_id") or t.get("id") or ""
        sender = t.get("sender", "")
        subject = t.get("subject", "(no subject)")
        date = t.get("date", "")
        snippet = t.get("snippet", "")

        # engine.py gives us one normalized message per Gmail thread; we
        # wrap it as a single-message thread in our UI shape so the rest
        # of the app is consistent.
        converted.append({
            "id": thread_id,
            "subject": subject,
            "messages": [
                {
                    "from": sender,
                    "date": date,
                    "body": snippet,
                }
            ],
        })
    return converted


def triage_threads(threads: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Run triage_inbox() over `threads` and group results by priority.

    triage_inbox() expects dicts with {sender, subject, snippet} and
    returns a sorted list of `{...thread, priority, category, reason}`
    dicts. We adapt our UI-shape threads into that input, then bucket the
    output back into a dict keyed by priority.
    """
    triage_inbox = _get_triage()

    # Adapt: build the minimal {sender, subject, snippet} view that
    # triage.py expects, while preserving the original thread so we can
    # attach the triage metadata without losing message history.
    triage_input: list[dict[str, Any]] = []
    for t in threads:
        messages = t.get("messages") or []
        first = messages[0] if messages else {}
        triage_input.append({
            "_thread": t,                              # keep our UI thread around
            "sender": first.get("from", ""),
            "subject": t.get("subject", ""),
            "snippet": _preview_from_body(first.get("body", ""), limit=200),
        })

    # Triage. This calls Gemini once per thread — may take a few seconds.
    raw_results = triage_inbox(triage_input)

    # Group by priority. Anything triage.py doesn't recognize falls into
    # "ignore" so the UI never crashes on an unexpected label.
    grouped: dict[str, list[dict[str, Any]]] = {p: [] for p in PRIORITY_ORDER}
    for r in raw_results:
        priority = (r.get("priority") or "ignore").lower()
        if priority not in grouped:
            priority = "ignore"
        thread = r.get("_thread", {})
        # Attach triage metadata onto our UI thread (don't mutate the
        # underlying session_state list element directly — we stored it).
        enriched = {
            **thread,
            "_priority": priority,
            "_category": r.get("category", "other"),
            "_reason": r.get("reason", ""),
        }
        grouped[priority].append(enriched)

    return grouped


# ---------------------------------------------------------------------------
# Full pipeline (fetch → triage → draft all, no UI)
# ---------------------------------------------------------------------------

def run_full_pipeline() -> list[str]:
    """Run the complete fetch → triage → draft pipeline without rendering UI.

    Reads ``st.session_state.source`` to decide where threads come from,
    runs every stage end-to-end, stores all results in session state, and
    advances ``current_phase`` to ``"Approval Gate"`` so the user lands
    on the review screen.

    Errors at any stage are caught, appended to the log, and execution
    continues where possible — a single draft failure never aborts the
    remaining drafts.

    Returns
    -------
    list[str]
        Ordered log lines describing what happened (suitable for display
        in a ``st.code`` block or similar). Each line is prefixed with
        ``[OK]``, ``[WARN]``, or ``[ERROR]``.
    """
    log: list[str] = []
    source = st.session_state.source

    # ------------------------------------------------------------------
    # Step 1: Fetch threads
    # ------------------------------------------------------------------
    log.append(f"[OK] Source: {source}")
    try:
        if source == "Sample threads":
            threads = load_sample_threads()
            if not threads:
                log.append(
                    f"[ERROR] No threads found at {SAMPLE_THREADS_PATH.name}."
                )
                return log
        else:
            threads = fetch_threads_via_engine()
            if not threads:
                log.append("[WARN] Gmail returned 0 threads.")
                return log
        log.append(f"[OK] Fetched {len(threads)} thread(s).")
    except Exception as exc:  # noqa: BLE001
        log.append(f"[ERROR] Fetch failed: {exc}")
        return log

    st.session_state.threads = threads

    # ------------------------------------------------------------------
    # Step 2: Triage
    # ------------------------------------------------------------------
    try:
        grouped = triage_threads(threads)
        st.session_state.triaged = grouped
        urgent_n = len(grouped.get("urgent", []))
        needs_reply_n = len(grouped.get("needs-reply", []))
        total_n = sum(len(v) for v in grouped.values())
        log.append(
            f"[OK] Triaged {total_n} thread(s): "
            f"{urgent_n} urgent, {needs_reply_n} need reply."
        )
    except Exception as exc:  # noqa: BLE001
        log.append(f"[ERROR] Triage failed: {exc}")
        # Keep threads in state so the inbox phase can show them.
        st.session_state.triaged = {}
        return log

    # ------------------------------------------------------------------
    # Step 3: Reset downstream state
    # ------------------------------------------------------------------
    st.session_state.drafts = {}
    st.session_state.approved = {}
    st.session_state.rejected = set()
    st.session_state.sent = set()
    st.session_state.booked = {}
    log.append("[OK] Downstream state reset.")

    # ------------------------------------------------------------------
    # Step 4: Draft actionable threads
    # ------------------------------------------------------------------
    actionable = (
        grouped.get("urgent", []) + grouped.get("needs-reply", [])
    )

    if not actionable:
        log.append("[WARN] No urgent or needs-reply threads — nothing to draft.")
    else:
        log.append(f"[OK] Drafting {len(actionable)} thread(s)…")
        try:
            draft_reply = _get_draft_reply()
        except Exception as exc:  # noqa: BLE001
            log.append(f"[ERROR] Could not load draft_reply: {exc}")
            return log

        ok_count = 0
        for i, thread in enumerate(actionable):
            thread_id = thread.get("id", f"thread_{i}")
            subject = thread.get("subject", "(no subject)")
            try:
                draft = draft_reply(thread)
                st.session_state.drafts[thread_id] = draft
                log.append(f"[OK] Draft {i + 1}/{len(actionable)}: {subject[:60]}")
                ok_count += 1
            except Exception as exc:  # noqa: BLE001
                st.session_state.drafts[thread_id] = f"[Draft failed: {exc}]"
                log.append(
                    f"[ERROR] Draft {i + 1}/{len(actionable)} failed "
                    f"({subject[:50]}): {exc}"
                )

        log.append(
            f"[OK] Drafting complete: {ok_count}/{len(actionable)} succeeded."
        )

    # ------------------------------------------------------------------
    # Step 5: Advance phase
    # ------------------------------------------------------------------
    st.session_state.current_phase = "Approval Gate"
    log.append("[OK] Phase → Approval Gate.")

    return log


# ---------------------------------------------------------------------------
# Pipeline execution with live UI
# ---------------------------------------------------------------------------

def _render_pipeline_execution() -> None:
    """Run the full fetch → triage → draft pipeline with live status UI.

    Uses ``st.status`` to show real-time progress. Each step updates the
    status label while running and writes a result line on completion.
    On fatal error the status collapses to the error state and returns
    early. On full success it collapses to the complete state, then
    advances the phase and reruns.
    """
    source = st.session_state.source
    log: list[str] = []

    with st.status("Running full pipeline…", expanded=True) as status:

        # ------------------------------------------------------------------
        # Step 1: Fetch
        # ------------------------------------------------------------------
        status.update(label="Step 1/3 — Fetching threads…")
        try:
            if source == "Sample threads":
                threads = load_sample_threads()
                if not threads:
                    msg = f"No threads found at {SAMPLE_THREADS_PATH.name}."
                    st.write(f"❌ Fetch: {msg}")
                    log.append(f"[ERROR] {msg}")
                    status.update(label="Pipeline failed — no threads found.", state="error")
                    st.session_state.pipeline_running = False
                    return
            else:
                threads = fetch_threads_via_engine()
                if not threads:
                    msg = "Gmail returned 0 threads."
                    st.write(f"⚠️ Fetch: {msg}")
                    log.append(f"[WARN] {msg}")
                    status.update(label="Pipeline stopped — inbox is empty.", state="error")
                    st.session_state.pipeline_running = False
                    return

            st.session_state.threads = threads
            line = f"✅ Fetched {len(threads)} thread(s) from {source}."
            st.write(line)
            log.append(f"[OK] {line}")

        except Exception as exc:  # noqa: BLE001
            msg = f"Fetch failed: {exc}"
            st.write(f"❌ {msg}")
            log.append(f"[ERROR] {msg}")
            status.update(label="Pipeline failed — could not fetch threads.", state="error")
            st.session_state.pipeline_running = False
            return

        # ------------------------------------------------------------------
        # Step 2: Triage
        # ------------------------------------------------------------------
        status.update(label="Step 2/3 — Triaging threads…")
        try:
            grouped = triage_threads(threads)
            st.session_state.triaged = grouped

            urgent_n = len(grouped.get("urgent", []))
            needs_reply_n = len(grouped.get("needs-reply", []))
            total_n = sum(len(v) for v in grouped.values())

            line = (
                f"✅ Triaged {total_n} thread(s): "
                f"{urgent_n} urgent · {needs_reply_n} need reply."
            )
            st.write(line)
            log.append(f"[OK] {line}")

        except Exception as exc:  # noqa: BLE001
            msg = f"Triage failed: {exc}"
            st.write(f"❌ {msg}")
            log.append(f"[ERROR] {msg}")
            status.update(label="Pipeline failed — triage error.", state="error")
            st.session_state.pipeline_running = False
            return

        # Reset downstream state after a successful triage
        st.session_state.drafts = {}
        st.session_state.approved = {}
        st.session_state.rejected = set()
        st.session_state.sent = set()
        st.session_state.booked = {}

        # ------------------------------------------------------------------
        # Step 3: Draft loop
        # ------------------------------------------------------------------
        actionable = (
            grouped.get("urgent", []) + grouped.get("needs-reply", [])
        )

        if not actionable:
            line = "⚠️ No urgent or needs-reply threads — nothing to draft."
            st.write(line)
            log.append(f"[WARN] {line}")
        else:
            status.update(label=f"Step 3/3 — Drafting {len(actionable)} thread(s)…")

            try:
                draft_reply = _get_draft_reply()
            except Exception as exc:  # noqa: BLE001
                msg = f"Could not load draft_reply: {exc}"
                st.write(f"❌ {msg}")
                log.append(f"[ERROR] {msg}")
                status.update(label="Pipeline failed — draft engine unavailable.", state="error")
                st.session_state.pipeline_running = False
                return

            ok_count = 0
            for i, thread in enumerate(actionable):
                thread_id = thread.get("id", f"thread_{i}")
                subject = thread.get("subject", "(no subject)")
                status.update(
                    label=(
                        f"Step 3/3 — Drafting {i + 1}/{len(actionable)}: "
                        f"{subject[:55]}…"
                    )
                )
                try:
                    draft = draft_reply(thread)
                    st.session_state.drafts[thread_id] = draft
                    st.write(f"✅ Draft {i + 1}/{len(actionable)}: {subject[:60]}")
                    log.append(f"[OK] Draft {i + 1}/{len(actionable)}: {subject[:60]}")
                    ok_count += 1
                except Exception as exc:  # noqa: BLE001
                    st.session_state.drafts[thread_id] = f"[Draft failed: {exc}]"
                    st.write(f"❌ Draft {i + 1}/{len(actionable)} failed ({subject[:45]}): {exc}")
                    log.append(
                        f"[ERROR] Draft {i + 1}/{len(actionable)} failed "
                        f"({subject[:50]}): {exc}"
                    )
                    # Continue — don't abort remaining drafts

            line = f"✅ Drafting complete: {ok_count}/{len(actionable)} succeeded."
            st.write(line)
            log.append(f"[OK] {line}")

        status.update(label="Pipeline complete — reviewing drafts.", state="complete")

    # Outside the status block — state changes + rerun
    st.session_state.pipeline_log = log
    st.session_state.current_phase = "Approval Gate"
    st.session_state.pipeline_running = False
    st.rerun()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def render_thread_card(thread: dict[str, Any]) -> None:
    """Render a single thread as a compact, expandable card."""
    subject = thread.get("subject", "(no subject)")
    thread_id = thread.get("id", "?")
    messages = thread.get("messages", []) or []

    first = messages[0] if messages else {}
    sender = first.get("from", "Unknown sender")
    date = first.get("date", "")
    preview = _preview_from_body(first.get("body", ""), limit=180)
    reason = thread.get("_reason", "")

    with st.container(border=True):
        col_a, col_b = st.columns([4, 1])
        with col_a:
            st.markdown(f"**{subject}**")
            st.caption(
                f"From: {sender}  •  {date}  •  {len(messages)} message(s)"
            )
            if reason:
                st.caption(f"_Why: {reason}_")
            st.write(preview)
        with col_b:
            st.caption(f"ID: `{thread_id}`")
            with st.popover("Open"):
                for i, msg in enumerate(messages, start=1):
                    st.markdown(
                        f"**{i}. {msg.get('from', '?')}** "
                        f"· {msg.get('date', '')}"
                    )
                    st.write(msg.get("body", ""))
                    if i < len(messages):
                        st.divider()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def render_sidebar() -> None:
    with st.sidebar:
        st.title("✍️ The Draft Desk")
        st.caption("Chief of Staff — Draft workflow")
        st.divider()

        if st.button(
            "⚡ Run Full Pipeline",
            type="primary",
            use_container_width=True,
            key="run_pipeline_btn",
        ):
            st.session_state.pipeline_running = True
            st.rerun()
        st.caption("Fetches, triages, and drafts — stops at Approval Gate.")

        st.divider()
        st.subheader("Source")
        st.session_state.source = st.radio(
            "Where do threads come from?",
            options=["Sample threads", "Gmail via engine.py"],
            index=0 if st.session_state.source == "Sample threads" else 1,
            label_visibility="collapsed",
            key="source_radio",
        )

        if st.session_state.source == "Gmail via engine.py":
            st.caption("Pulls live threads via engine.fetch_threads().")

        st.divider()
        st.subheader("Navigation")

        for phase in PHASES:
            is_current = st.session_state.current_phase == phase
            label = f"▶ {phase}" if is_current else phase
            if st.button(label, key=f"nav_{phase}", use_container_width=True):
                st.session_state.current_phase = phase
                st.rerun()

        st.divider()
        triaged_total = sum(len(v) for v in st.session_state.triaged.values())
        st.caption(
            f"Loaded: **{len(st.session_state.threads)}** thread(s)  \n"
            f"Triaged: **{triaged_total}**  \n"
            f"Drafts: **{len(st.session_state.drafts)}**  \n"
            f"Approved: **{len(st.session_state.approved)}**  \n"
            f"Rejected: **{len(st.session_state.rejected)}**  \n"
            f"Sent: **{len(st.session_state.sent)}**  \n"
            f"Booked: **{len(st.session_state.booked)}**"
        )


# ---------------------------------------------------------------------------
# Phase: Inbox & Triage
# ---------------------------------------------------------------------------
def _do_pull_and_triage() -> tuple[list[dict[str, Any]] | None, str | None]:
    """
    Pull threads from the selected source, run triage, and store results
    in session_state. Returns (loaded_threads_or_None, summary_message_or_None).
    """
    source = st.session_state.source

    # 1) Load threads (UI shape)
    try:
        if source == "Sample threads":
            threads = load_sample_threads()
            if not threads:
                return None, (
                    f"error: No threads found at `{SAMPLE_THREADS_PATH.name}`. "
                    "Make sure the file exists and is valid JSON."
                )
        else:
            threads = fetch_threads_via_engine()
            if not threads:
                return None, "info: Gmail returned 0 threads."
    except Exception as e:  # noqa: BLE001 — surface anything to the UI
        return None, f"error: Failed to pull from {source}: {e}"

    # 2) Triage. Errors here usually mean GEMINI_API_KEY isn't set.
    try:
        grouped = triage_threads(threads)
    except Exception as e:  # noqa: BLE001
        # We still keep the raw threads loaded so the user can see them.
        st.session_state.threads = threads
        st.session_state.triaged = {}
        return threads, (
            f"error: Loaded {len(threads)} thread(s) but triage failed: {e}"
        )

    # 3) Persist.
    st.session_state.threads = threads
    st.session_state.triaged = grouped
    # Drafting state is downstream — reset on every fresh pull.
    st.session_state.drafts = {}
    st.session_state.approved = {}
    st.session_state.rejected = set()

    total = sum(len(v) for v in grouped.values())
    urgent = len(grouped.get("urgent", []))
    needs_reply = len(grouped.get("needs-reply", []))
    summary = (
        f"success: Pulled {len(threads)} thread(s) from {source}. "
        f"Triaged {total}: 🚨 {urgent} urgent · 💬 {needs_reply} need reply."
    )
    return threads, summary


def _flash_summary(summary: str | None) -> None:
    if not summary:
        return
    kind, _, message = summary.partition(":")
    if kind == "error":
        st.error(message.strip())
    elif kind == "info":
        st.info(message.strip())
    else:
        st.success(message.strip())


def render_inbox_phase() -> None:
    st.header("📥 Inbox & Triage")
    st.write(
        "Pull threads from the selected source, then triage them by priority. "
        "Once triaged, the highest-priority threads move to *Draft Generation*."
    )

    col1, col2, col3 = st.columns([1, 1, 4])
    with col1:
        pull_clicked = st.button(
            "Pull & Triage",
            type="primary",
            use_container_width=True,
        )
    with col2:
        if st.button("Clear", use_container_width=True):
            st.session_state.threads = []
            st.session_state.triaged = {}
            st.session_state.drafts = {}
            st.session_state.approved = {}
            st.session_state.rejected = set()
            st.session_state.last_pull_summary = None
            st.rerun()

    if pull_clicked:
        with st.spinner("Pulling & triaging…"):
            _, summary = _do_pull_and_triage()
        st.session_state.last_pull_summary = summary
        st.rerun()

    _flash_summary(st.session_state.last_pull_summary)

    st.divider()

    triaged: dict[str, list[dict[str, Any]]] = st.session_state.triaged or {}
    has_triaged = any(triaged.get(p) for p in PRIORITY_ORDER)

    if not has_triaged:
        # Fallback: show raw threads if we have them but triage is empty
        # (e.g. triage errored but pull succeeded).
        threads = st.session_state.threads
        if threads:
            st.warning(
                "Triage didn't produce any buckets. Showing raw threads "
                "below — pull again once the issue is resolved."
            )
            _render_raw_threads(threads)
        else:
            st.info("No threads loaded yet. Click **Pull & Triage** to get started.")
        return

    # Sort options for each bucket
    sort_options = ["Most recent first", "Oldest first", "Most messages first"]
    sort_by = st.selectbox("Sort by", options=sort_options, index=0, key="sort_by")

    # Track how many need a reply so we can show the footer CTA
    needs_reply_count = len(triaged.get("needs-reply", []))
    urgent_count = len(triaged.get("urgent", []))

    for priority in PRIORITY_ORDER:
        bucket = triaged.get(priority, [])
        if not bucket:
            continue
        header, blurb = PRIORITY_META[priority]
        st.subheader(f"{header}  ({len(bucket)})")
        st.caption(blurb)

        sorted_bucket = _sort_threads(bucket, sort_by)
        for thread in sorted_bucket:
            label = f"{thread.get('subject', '(no subject)')}"
            reason = thread.get("_reason", "")
            category = thread.get("_category", "")
            if reason:
                label += f"  —  _{reason}_"
            if category:
                label += f"  ·  `{category}`"
            with st.expander(label, expanded=False):
                for i, msg in enumerate(thread.get("messages", []), start=1):
                    st.markdown(
                        f"**{i}. {msg.get('from', '?')}** "
                        f"· {msg.get('date', '')}"
                    )
                    st.write(msg.get("body", ""))
                    if i < len(thread.get("messages", [])):
                        st.divider()
                st.caption(f"Thread ID: `{thread.get('id', '?')}`")

    # Footer CTA — count of threads that warrant a reply
    reply_total = urgent_count + needs_reply_count
    st.divider()
    if reply_total > 0:
        st.success(
            f"**{reply_total}** thread(s) need a reply → go to **Draft Generation**"
        )
    else:
        st.info("Nothing needs a reply right now. 🎉")


def _sort_threads(
    threads: list[dict[str, Any]], sort_by: str
) -> list[dict[str, Any]]:
    sorted_threads = list(threads)
    if sort_by == "Most recent first":
        sorted_threads.sort(
            key=lambda t: (t.get("messages") or [{}])[0].get("date", ""),
            reverse=True,
        )
    elif sort_by == "Oldest first":
        sorted_threads.sort(
            key=lambda t: (t.get("messages") or [{}])[0].get("date", ""),
        )
    else:  # Most messages first
        sorted_threads.sort(
            key=lambda t: len(t.get("messages") or []),
            reverse=True,
        )
    return sorted_threads


def _render_raw_threads(threads: list[dict[str, Any]]) -> None:
    """Fallback renderer when triage failed but threads are loaded."""
    for thread in threads:
        render_thread_card(thread)


# ---------------------------------------------------------------------------
# Phase: Draft Generation
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _get_draft_reply():
    """Import draft_reply once and cache it."""
    from draft_machine import draft_reply  # type: ignore
    return draft_reply


def render_draft_phase() -> None:
    st.header("📝 Draft Generation")
    st.write(
        "Generate AI drafts for every thread that needs a reply. "
        "Drafts are built from your tone profile and past replies."
    )

    triaged: dict[str, list[dict[str, Any]]] = st.session_state.triaged or {}
    actionable: list[dict[str, Any]] = (
        triaged.get("urgent", []) + triaged.get("needs-reply", [])
    )

    if not actionable:
        st.warning(
            "No actionable threads found. "
            "Go back to **Inbox & Triage** and run **Pull & Triage** first."
        )
        return

    already_drafted = len(st.session_state.drafts)
    st.caption(
        f"{len(actionable)} thread(s) need a reply · "
        f"{already_drafted} draft(s) already generated"
    )

    generate_clicked = st.button(
        "⚡ Generate All Drafts",
        type="primary",
        disabled=(already_drafted == len(actionable)),
    )

    if generate_clicked:
        draft_reply = _get_draft_reply()
        progress_bar = st.progress(0, text="Starting…")
        errors: list[str] = []

        for i, thread in enumerate(actionable):
            thread_id = thread.get("id", f"thread_{i}")
            subject = thread.get("subject", "(no subject)")
            progress_bar.progress(
                i / len(actionable),
                text=f"Drafting {i + 1}/{len(actionable)}: {subject[:60]}…",
            )
            try:
                draft = draft_reply(thread)
                st.session_state.drafts[thread_id] = draft
            except Exception as exc:  # noqa: BLE001
                errors.append(f"**{subject}**: {exc}")
                st.session_state.drafts[thread_id] = (
                    f"[Draft failed: {exc}]"
                )

        progress_bar.progress(1.0, text="Done ✓")

        if errors:
            st.error("Some drafts failed:\n" + "\n".join(f"- {e}" for e in errors))
        else:
            st.success(
                f"All {len(actionable)} draft(s) generated — "
                "review below, then head to **Approval Gate**."
            )
        st.rerun()

    # ---------------------------------------------------------------------------
    # Display draft cards
    # ---------------------------------------------------------------------------
    drafts: dict[str, str] = st.session_state.drafts

    if not drafts:
        st.info("Click **Generate All Drafts** to create drafts for the threads above.")
        return

    st.divider()
    st.subheader(f"Drafts ({len(drafts)})")

    for thread in actionable:
        thread_id = thread.get("id", "")
        subject = thread.get("subject", "(no subject)")
        priority = thread.get("_priority", "needs-reply")
        priority_badge = "🚨" if priority == "urgent" else "💬"

        if thread_id not in drafts:
            continue

        with st.expander(f"{priority_badge} {subject}", expanded=False):
            col_left, col_right = st.columns(2)

            messages = thread.get("messages") or []
            latest_msg = messages[-1] if messages else {}

            with col_left:
                st.markdown("**Original thread (latest message)**")
                st.caption(
                    f"From: {latest_msg.get('from', 'Unknown')}  •  "
                    f"{latest_msg.get('date', '')}"
                )
                st.write(latest_msg.get("body", "_(no body)_"))
                if len(messages) > 1:
                    st.caption(f"_{len(messages)} message(s) in thread_")

            with col_right:
                st.markdown("**AI-generated draft**")
                draft_text = drafts[thread_id]
                st.text_area(
                    label="Draft",
                    value=draft_text,
                    height=220,
                    key=f"draft_text_{thread_id}",
                    label_visibility="collapsed",
                )
                st.caption(f"{len(draft_text)} chars · Thread ID: `{thread_id}`")

    st.divider()
    st.success("✅ Drafts ready → go to **Approval Gate**")


# ---------------------------------------------------------------------------
# Phase: Approval Gate
# ---------------------------------------------------------------------------
def _actionable_threads_by_id() -> dict[str, dict[str, Any]]:
    """Return a {thread_id: thread} map for urgent + needs-reply threads."""
    triaged: dict[str, list[dict[str, Any]]] = st.session_state.triaged or {}
    actionable = triaged.get("urgent", []) + triaged.get("needs-reply", [])
    return {t.get("id", f"t_{i}"): t for i, t in enumerate(actionable)}


def render_approval_phase() -> None:
    st.header("✅ Approval Gate")
    st.write(
        "Review each AI draft. Approve (optionally edited), regenerate, or reject. "
        "Nothing moves to Export until you explicitly approve it."
    )

    # ---- Pipeline execution log ----
    pipeline_log: list[str] = st.session_state.pipeline_log or []
    if pipeline_log:
        with st.expander("🗒️ Pipeline Execution Log", expanded=False):
            for entry in pipeline_log:
                upper = entry.upper()
                if "ERROR" in upper or "FAILED" in upper:
                    st.write(f"❌ {entry}")
                else:
                    st.write(f"✅ {entry}")
            if st.button("Clear log", key="clear_pipeline_log"):
                st.session_state.pipeline_log = []
                st.rerun()
        st.divider()

    drafts: dict[str, str] = st.session_state.drafts
    approved: dict[str, str] = st.session_state.approved
    rejected: set[str] = st.session_state.rejected
    sent: set[str] = st.session_state.sent
    booked: dict[str, dict] = st.session_state.booked

    if not drafts:
        st.warning(
            "No drafts to review yet. "
            "Go back to **Draft Generation** and generate drafts first."
        )
        return

    threads_by_id = _actionable_threads_by_id()

    # Running totals banner
    pending_ids = [
        tid for tid in drafts
        if tid not in approved and tid not in rejected
    ]
    col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
    col_stat1.metric("Total drafts", len(drafts))
    col_stat2.metric("✅ Approved", len(approved))
    col_stat3.metric("❌ Rejected", len(rejected))
    col_stat4.metric("⏳ Pending", len(pending_ids))

    st.divider()

    all_reviewed = len(pending_ids) == 0

    # ---- Iterate over every draft in insertion order ----
    for thread_id, original_draft in drafts.items():
        thread = threads_by_id.get(thread_id, {})
        subject = thread.get("subject", thread_id)
        messages = thread.get("messages") or []
        priority = thread.get("_priority", "needs-reply")
        badge = "🚨" if priority == "urgent" else "💬"

        is_approved = thread_id in approved
        is_rejected = thread_id in rejected
        is_sent = thread_id in sent

        if is_sent:
            status_label = "📤 Sent"
            expanded = False
        elif is_approved:
            status_label = "✅ Approved"
            expanded = False
        elif is_rejected:
            status_label = "❌ Rejected"
            expanded = False
        else:
            status_label = "⏳ Pending review"
            expanded = True  # open pending items by default

        with st.expander(
            f"{badge} {subject}  —  {status_label}", expanded=expanded
        ):
            col_left, col_right = st.columns(2)

            # ---- Left: full thread ----
            with col_left:
                st.markdown("**Email thread**")
                for i, msg in enumerate(messages, start=1):
                    st.markdown(
                        f"**{i}. {msg.get('from', '?')}** · {msg.get('date', '')}"
                    )
                    st.write(msg.get("body", "_(no body)_"))
                    if i < len(messages):
                        st.divider()

            # ---- Right: editable draft + action buttons ----
            with col_right:
                st.markdown("**Draft reply**")

                # Use approved text if already approved, else the draft
                display_text = approved.get(thread_id, original_draft)

                edited = st.text_area(
                    label="draft",
                    value=display_text,
                    height=260,
                    key=f"approval_text_{thread_id}",
                    label_visibility="collapsed",
                    disabled=is_approved or is_rejected,
                )

                if not is_approved and not is_rejected:
                    btn_a, btn_b, btn_c = st.columns(3)

                    with btn_a:
                        if st.button(
                            "✅ Approve",
                            key=f"approve_{thread_id}",
                            type="primary",
                            use_container_width=True,
                        ):
                            final = (edited or "").strip() or original_draft
                            st.session_state.approved[thread_id] = final
                            st.rerun()

                    with btn_b:
                        if st.button(
                            "🔄 Regenerate",
                            key=f"regen_{thread_id}",
                            use_container_width=True,
                        ):
                            draft_reply = _get_draft_reply()
                            with st.spinner("Regenerating…"):
                                try:
                                    new_draft = draft_reply(thread)
                                    st.session_state.drafts[thread_id] = new_draft
                                    # Clear any prior rejection so it shows as pending
                                    st.session_state.rejected.discard(thread_id)
                                    st.session_state.approved.pop(thread_id, None)
                                except Exception as exc:  # noqa: BLE001
                                    st.error(f"Regeneration failed: {exc}")
                            st.rerun()

                    with btn_c:
                        if st.button(
                            "❌ Reject",
                            key=f"reject_{thread_id}",
                            use_container_width=True,
                        ):
                            st.session_state.rejected.add(thread_id)
                            st.rerun()

                elif is_approved:
                    if is_sent:
                        st.success("📤 Sent successfully.")
                    else:
                        st.success("Approved — ready to send or export.")
                        # Extract recipient e-mail from last message "from" field,
                        # handling both "Name <email@host>" and bare "email@host".
                        last_from = (messages[-1].get("from", "") if messages else "")
                        if "<" in last_from and ">" in last_from:
                            recipient = last_from.split("<", 1)[1].rstrip(">").strip()
                        else:
                            recipient = last_from.strip()

                        is_meeting = thread.get("_category", "") == "meeting-request"
                        is_booked = thread_id in st.session_state.booked

                        if is_meeting and is_booked:
                            event = st.session_state.booked[thread_id]
                            cal_link = event.get("htmlLink", "")
                            st.info(
                                f"📅 Meeting booked."
                                + (f"  [Open in Calendar]({cal_link})" if cal_link else "")
                            )

                        # --- action buttons ---
                        if is_meeting:
                            send_col, book_col = st.columns(2)
                        else:
                            send_col, _ = st.columns([1, 2])

                        with send_col:
                            if st.button(
                                "📤 Send",
                                key=f"send_{thread_id}",
                                type="primary",
                                use_container_width=True,
                                disabled=not recipient,
                            ):
                                send_reply = _get_send_reply()
                                draft_body = approved[thread_id]
                                with st.spinner(f"Sending to {recipient}……"):
                                    try:
                                        result = send_reply(
                                            thread_id=thread_id,
                                            to=recipient,
                                            subject=thread.get("subject", ""),
                                            body=draft_body,
                                        )
                                        st.session_state.sent.add(thread_id)
                                        st.success(f"Sent to {recipient}.")
                                        print(result)
                                        if result.get("thread_id"):
                                            log_action(
                                                action_type="sent",
                                                thread_subject=thread.get("subject", ""),
                                                detail=recipient,
                                                action_id=result["thread_id"],
                                            )
                                    except Exception as exc:  # noqa: BLE001
                                        st.error(f"Send failed: {exc}")
                                st.rerun()

                        if is_meeting and not is_booked:
                            # Show any error from the previous booking attempt
                            # (stored in session state so it survives reruns).
                            book_err_key = f"_book_error_{thread_id}"
                            if st.session_state.get(book_err_key):
                                st.error(st.session_state[book_err_key])
                                if st.button(
                                    "Dismiss",
                                    key=f"dismiss_book_err_{thread_id}",
                                ):
                                    st.session_state[book_err_key] = None
                                    st.rerun()

                            with book_col:
                                if st.button(
                                    "📅 Book Meeting",
                                    key=f"book_{thread_id}",
                                    use_container_width=True,
                                ):
                                    parse_meeting_request, find_free_slot, create_event = (
                                        _get_calendar_engine()
                                    )
                                    # Clear any previous error before this attempt.
                                    st.session_state[book_err_key] = None

                                    # Fetch the full thread body on demand — the
                                    # session_state copy only has a short snippet.
                                    # Fall back to the cached thread if fetch fails.
                                    with st.spinner("Fetching full email body…"):
                                        try:
                                            fetch_full_thread = _get_fetch_full_thread()
                                            full_thread = fetch_full_thread(thread_id)
                                        except Exception:  # noqa: BLE001
                                            full_thread = thread  # best-effort fallback

                                    with st.spinner("Parsing meeting details…"):
                                        meeting = parse_meeting_request(full_thread)

                                    if "parsing_error" in meeting:
                                        st.session_state[book_err_key] = (
                                            f"Could not parse meeting details: "
                                            f"{meeting['parsing_error']}"
                                        )
                                        st.rerun()
                                    else:
                                        proposed = meeting.get("proposed_times", [])
                                        duration = meeting.get("duration_minutes", 30)
                                        topic = meeting.get("topic", thread.get("subject", ""))
                                        invitees = meeting.get("attendees", [])

                                        st.info(
                                            f"**Topic:** {topic}  \n"
                                            f"**Duration:** {duration} min  \n"
                                            f"**Proposed times:** "
                                            + (", ".join(proposed) if proposed else "_none found_")
                                            + f"  \n**Attendees:** "
                                            + (", ".join(invitees) if invitees else "_none found_")
                                        )

                                        if not proposed:
                                            st.session_state[book_err_key] = (
                                                "No proposed times found in the thread — "
                                                "cannot check availability."
                                            )
                                            st.rerun()
                                        else:
                                            with st.spinner("Checking calendar availability…"):
                                                free_slot = find_free_slot(proposed, duration)

                                            if free_slot is None:
                                                st.session_state[book_err_key] = (
                                                    "None of the proposed times are free. "
                                                    "Reply to suggest alternatives."
                                                )
                                                st.rerun()
                                            else:
                                                with st.spinner(f"Booking {free_slot}…"):
                                                    try:
                                                        event = create_event(
                                                            summary=topic,
                                                            start_time=free_slot,
                                                            duration_minutes=duration,
                                                            attendees=invitees,
                                                            description=approved.get(thread_id, ""),
                                                        )
                                                        st.session_state.booked[thread_id] = event
                                                        # Success — rerun to show the
                                                        # booked state (link replaces button).
                                                        print(event)
                                                        if event.get("id"):
                                                            log_action(
                                                                action_type="booked",
                                                                thread_subject=thread.get("subject", ""),
                                                                detail=topic,
                                                                action_id=event["id"],
                                                            )
                                                        st.rerun()
                                                    except Exception as exc:  # noqa: BLE001
                                                        st.session_state[book_err_key] = (
                                                            f"Booking failed: {exc}"
                                                        )
                                                        st.rerun()

                        if not recipient:
                            st.caption("⚠️ No recipient address found — cannot send.")
                else:
                    st.error("Rejected — regenerate to try again.")

    # ---- All-reviewed state ----
    st.divider()
    if all_reviewed:
        if approved:
            st.balloons()
            st.success(
                f"🎉 All {len(drafts)} draft(s) reviewed — "
                f"**{len(approved)}** approved · **{len(rejected)}** rejected · "
                f"**{len(sent)}** sent · **{len(booked)}** booked. "
                "Head to **Export Proof** to download your session proof."
            )
        else:
            st.info("All drafts reviewed but none were approved.")
    elif pending_ids:
        st.info(f"**{len(pending_ids)}** draft(s) still need review.")


# ---------------------------------------------------------------------------
# Phase: Export Proof
# ---------------------------------------------------------------------------
def _quote_thread(messages: list[dict[str, Any]]) -> str:
    """Format all thread messages as a plain-text quoted block."""
    lines: list[str] = []
    for msg in messages:
        lines.append(f"From: {msg.get('from', '?')}")
        lines.append(f"Date: {msg.get('date', '?')}")
        lines.append("")
        for body_line in (msg.get("body") or "").splitlines():
            lines.append(f"    {body_line}")
        lines.append("")
    return "\n".join(lines).rstrip()


def generate_proof_markdown(
    approved: dict[str, str],
    threads_by_id: dict[str, dict[str, Any]],
) -> str:
    """Build a Markdown proof document for all approved drafts."""
    from datetime import datetime  # local import — already in stdlib

    lines: list[str] = []
    lines.append("# Draft Desk — Session Proof")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Approved drafts:** {len(approved)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, (thread_id, draft_text) in enumerate(approved.items(), start=1):
        thread = threads_by_id.get(thread_id, {})
        subject = thread.get("subject", thread_id)
        messages = thread.get("messages") or []

        lines.append(f"## {i}. {subject}")
        lines.append("")
        lines.append("### Original thread")
        lines.append("")
        lines.append("```")
        lines.append(_quote_thread(messages))
        lines.append("```")
        lines.append("")
        lines.append("### Approved draft")
        lines.append("")
        lines.append("```")
        lines.append(draft_text)
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def generate_proof_html(
    approved: dict[str, str],
    threads_by_id: dict[str, dict[str, Any]],
) -> str:
    """Build a styled dark-theme HTML proof document for all approved drafts."""
    from datetime import datetime  # local import

    def _esc(s: str) -> str:
        """Minimal HTML escaping."""
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
        )

    css = """
    <style>
      * { box-sizing: border-box; margin: 0; padding: 0; }
      body {
        background: #0e1117;
        color: #e6edf3;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        padding: 32px;
        line-height: 1.6;
      }
      h1 { font-size: 1.8em; margin-bottom: 4px; }
      h2 { font-size: 1.3em; margin: 40px 0 16px; }
      .meta { color: #8b949e; font-size: 0.9em; margin-bottom: 24px; }
      .thread-block {
        margin-bottom: 40px;
        border: 1px solid #30363d;
        border-radius: 8px;
        overflow: hidden;
      }
      .thread-header {
        background: #161b22;
        padding: 12px 20px;
        font-weight: 700;
        font-size: 1.05em;
        border-bottom: 1px solid #30363d;
      }
      .thread-index {
        color: #8b949e;
        font-weight: 400;
        margin-right: 8px;
      }
      .cols {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0;
      }
      .col-original {
        padding: 20px;
        border-right: 1px solid #30363d;
        border-left: 4px solid #e6722e;
      }
      .col-draft {
        padding: 20px;
        border-left: 4px solid #3fb950;
      }
      .col-label {
        font-size: 0.78em;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #8b949e;
        margin-bottom: 12px;
      }
      .col-original .col-label { color: #e6722e; }
      .col-draft   .col-label { color: #3fb950; }
      .message-block {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 4px;
        padding: 10px 14px;
        margin-bottom: 10px;
      }
      .msg-meta { font-size: 0.82em; color: #8b949e; margin-bottom: 6px; }
      .msg-sender { color: #58a6ff; font-weight: 600; }
      .msg-body { white-space: pre-wrap; font-size: 0.9em; }
      .draft-body {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 4px;
        padding: 12px 16px;
        white-space: pre-wrap;
        font-size: 0.9em;
      }
      /* Action log table */
      .action-log {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.9em;
        margin-top: 8px;
      }
      .action-log th {
        text-align: left;
        padding: 8px 14px;
        background: #161b22;
        color: #8b949e;
        font-size: 0.78em;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        border-bottom: 1px solid #30363d;
      }
      .action-log td {
        padding: 10px 14px;
        border-bottom: 1px solid #21262d;
        vertical-align: middle;
      }
      .action-log tr:last-child td { border-bottom: none; }
      .action-log tr:hover td { background: #161b22; }
      .badge-sent {
        display: inline-block;
        background: #1f6feb22;
        color: #58a6ff;
        border: 1px solid #1f6feb55;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 20px;
        font-size: 0.82em;
      }
      .badge-booked {
        display: inline-block;
        background: #3fb95022;
        color: #3fb950;
        border: 1px solid #3fb95055;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 20px;
        font-size: 0.82em;
      }
      .log-detail { font-family: monospace; font-size: 0.88em; color: #8b949e; }
      .log-ts { color: #8b949e; font-size: 0.84em; white-space: nowrap; }
      .no-actions { color: #8b949e; font-style: italic; padding: 16px 0; }
      .footer {
        margin-top: 48px;
        padding-top: 20px;
        border-top: 1px solid #30363d;
        color: #8b949e;
        font-size: 0.88em;
        text-align: center;
      }
      .badge {
        display: inline-block;
        background: #1f6feb;
        color: #e6edf3;
        font-weight: 700;
        padding: 2px 10px;
        border-radius: 20px;
        font-size: 0.85em;
        margin-left: 8px;
      }
      @media (max-width: 700px) {
        .cols { grid-template-columns: 1fr; }
        .col-original { border-right: none; border-bottom: 1px solid #30363d; }
      }
    </style>
    """

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---- Draft blocks ----
    blocks: list[str] = []
    for i, (thread_id, draft_text) in enumerate(approved.items(), start=1):
        thread = threads_by_id.get(thread_id, {})
        subject = _esc(thread.get("subject", thread_id))
        messages = thread.get("messages") or []

        # Build message HTML for the left column
        msg_html_parts: list[str] = []
        for msg in messages:
            sender = _esc(msg.get("from", "?"))
            date = _esc(msg.get("date", ""))
            body = _esc((msg.get("body") or "").strip())
            msg_html_parts.append(
                f'<div class="message-block">'
                f'  <div class="msg-meta">'
                f'    <span class="msg-sender">{sender}</span> &middot; {date}'
                f'  </div>'
                f'  <div class="msg-body">{body}</div>'
                f'</div>'
            )

        draft_escaped = _esc(draft_text)

        blocks.append(f"""
<div class="thread-block">
  <div class="thread-header">
    <span class="thread-index">#{i}</span>{subject}
  </div>
  <div class="cols">
    <div class="col-original">
      <div class="col-label">Original thread</div>
      {"".join(msg_html_parts)}
    </div>
    <div class="col-draft">
      <div class="col-label">Approved draft</div>
      <div class="draft-body">{draft_escaped}</div>
    </div>
  </div>
</div>""")

    # ---- Action log section ----
    action_log = get_action_log()

    if not action_log:
        action_log_html = '<p class="no-actions">No actions logged in this session.</p>'
    else:
        rows: list[str] = []
        for entry in action_log:
            a_type = entry.get("action_type", "")
            icon = "📨" if a_type == "sent" else "📅"
            badge_cls = "badge-sent" if a_type == "sent" else "badge-booked"

            # Format timestamp
            raw_ts = entry.get("timestamp", "")
            try:
                from datetime import timezone as _tz
                parse_ts = raw_ts.replace("Z", "+00:00") if raw_ts.endswith("Z") else raw_ts
                dt = datetime.fromisoformat(parse_ts)
                fmt_ts = dt.strftime("%b %d, %Y %I:%M %p")
            except (ValueError, AttributeError):
                fmt_ts = raw_ts

            rows.append(
                f'<tr>'
                f'<td><span class="{badge_cls}">{icon} {_esc(a_type.upper())}</span></td>'
                f'<td>{_esc(entry.get("thread_subject", ""))}</td>'
                f'<td class="log-detail">{_esc(entry.get("detail", ""))}</td>'
                f'<td class="log-ts">{_esc(fmt_ts)}</td>'
                f'</tr>'
            )

        action_log_html = f"""
<table class="action-log">
  <thead>
    <tr>
      <th>Action</th>
      <th>Thread</th>
      <th>Detail</th>
      <th>Timestamp</th>
    </tr>
  </thead>
  <tbody>
    {"".join(rows)}
  </tbody>
</table>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Draft Desk — Session Proof</title>
  {css}
</head>
<body>
  <h1>✍️ Draft Desk — Session Proof</h1>
  <div class="meta">
    Generated {now_str} &nbsp;·&nbsp;
    {len(approved)} approved draft(s)
  </div>
  {"".join(blocks)}
  <h2>📋 Action Log</h2>
  {action_log_html}
  
</body>
</html>"""
    return html


def render_export_phase() -> None:
    st.header("📤 Export Proof")
    st.write(
        "Preview all approved drafts side-by-side with their original threads, "
        "then download your session proof in Markdown or HTML."
    )

    approved: dict[str, str] = st.session_state.approved
    threads_by_id = _actionable_threads_by_id()

    if not approved:
        st.warning(
            "No approved drafts yet. "
            "Go to **Approval Gate** and approve at least one draft first."
        )
        return

    st.caption(f"{len(approved)} approved draft(s) ready to export.")

    # ---- Preview cards (side-by-side) ----
    st.subheader("Preview")
    for thread_id, draft_text in approved.items():
        thread = threads_by_id.get(thread_id, {})
        subject = thread.get("subject", thread_id)
        messages = thread.get("messages") or []
        latest = messages[-1] if messages else {}

        with st.expander(f"✅ {subject}", expanded=True):
            col_left, col_right = st.columns(2)

            with col_left:
                st.markdown("**Original thread**")
                for i, msg in enumerate(messages, start=1):
                    st.markdown(
                        f"**{i}. {msg.get('from', '?')}** · {msg.get('date', '')}"
                    )
                    st.write(msg.get("body", "_(no body)_"))
                    if i < len(messages):
                        st.divider()

            with col_right:
                st.markdown("**Approved draft**")
                st.code(draft_text, language=None)

    # ---- Generate & download ----
    st.divider()
    st.subheader("Download")

    md_content = generate_proof_markdown(approved, threads_by_id)
    html_content = generate_proof_html(approved, threads_by_id)

    from datetime import datetime as _dt
    timestamp = _dt.now().strftime("%Y%m%d_%H%M")

    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        st.download_button(
            label="⬇️ Download Proof (Markdown)",
            data=md_content,
            file_name=f"draft_proof_{timestamp}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with dl_col2:
        st.download_button(
            label="⬇️ Download Proof (HTML)",
            data=html_content,
            file_name=f"draft_proof_{timestamp}.html",
            mime="text/html",
            use_container_width=True,
        )

    st.divider()
   

    # ---- Action Log ----
    st.divider()
    st.subheader("Action Log")

    action_log = get_action_log()

    if not action_log:
        st.info("No actions logged yet.")
    else:
        for entry in action_log:
            a_type = entry.get("action_type", "")
            icon = "📨" if a_type == "sent" else "📅"

            # Parse and reformat the timestamp — e.g. "Jan 01 02:30 PM"
            raw_ts = entry.get("timestamp", "")
            try:
                from datetime import datetime, timezone as _tz
                # Handle both +00:00 offset and trailing Z
                parse_ts = raw_ts.replace("Z", "+00:00") if raw_ts.endswith("Z") else raw_ts
                dt = datetime.fromisoformat(parse_ts)
                formatted_ts = dt.strftime("%b %d %I:%M %p")
            except (ValueError, AttributeError):
                formatted_ts = raw_ts

            col1, col2, col3, col4 = st.columns([1, 3, 3, 2])
            with col1:
                st.write(f"{icon} **{a_type.upper()}**")
            with col2:
                st.write(f"**{entry.get('thread_subject', '')}**")
            with col3:
                st.write(f"`{entry.get('detail', '')}`")
            with col4:
                st.caption(formatted_ts)


# ---------------------------------------------------------------------------
# Phase dispatch
# ---------------------------------------------------------------------------
def render_phase(phase: str) -> None:
    if phase == "Inbox & Triage":
        render_inbox_phase()
    elif phase == "Draft Generation":
        render_draft_phase()
    elif phase == "Approval Gate":
        render_approval_phase()
    elif phase == "Export Proof":
        render_export_phase()
    else:
        st.warning(f"Unknown phase: {phase}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    render_sidebar()
    if st.session_state.pipeline_running:
        _render_pipeline_execution()
    else:
        render_phase(st.session_state.current_phase)


if __name__ == "__main__":
    main()
