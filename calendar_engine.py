"""
calendar_engine.py
==================
Chief-of-Staff Google Calendar engine.

Provides ``_build_calendar_service()`` which returns an authenticated
``googleapiclient`` Resource for the Calendar v3 API.

OAuth credentials are shared with ``engine.py``:
  - ``credentials.json``  — OAuth desktop-client secrets
  - ``token.json``        — cached access/refresh token (auto-created)

The same three scopes used by engine.py are requested so that a single
token.json covers both Gmail and Calendar operations:
  - https://www.googleapis.com/auth/gmail.readonly
  - https://www.googleapis.com/auth/gmail.send
  - https://www.googleapis.com/auth/calendar
"""
from __future__ import annotations

import socket

# ---------------------------------------------------------------------------
# IPv4 monkey-patch (mirrors engine.py)
# ---------------------------------------------------------------------------
_original_getaddrinfo = socket.getaddrinfo


def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(
        host,
        port,
        socket.AF_INET,  # Force IPv4
        type,
        proto,
        flags,
    )


socket.getaddrinfo = ipv4_only_getaddrinfo

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Streamlit Cloud credential bootstrap (shared logic with engine.py)
# ---------------------------------------------------------------------------

def _bootstrap_credentials_from_secrets() -> None:
    """Write token.json / credentials.json from Streamlit secrets if absent."""
    here = Path(__file__).resolve().parent

    token_path = here / "token.json"
    if not token_path.exists():
        try:
            import streamlit as st  # type: ignore
            token_json = st.secrets.get("GOOGLE_TOKEN_JSON")
            if token_json:
                if isinstance(token_json, str):
                    token_path.write_text(token_json, encoding="utf-8")
                else:
                    token_path.write_text(
                        json.dumps(dict(token_json)), encoding="utf-8"
                    )
        except Exception:
            pass

    creds_path = here / "credentials.json"
    if not creds_path.exists():
        try:
            import streamlit as st  # type: ignore
            creds_json = st.secrets.get("GOOGLE_CREDENTIALS_JSON")
            if creds_json:
                if isinstance(creds_json, str):
                    creds_path.write_text(creds_json, encoding="utf-8")
                else:
                    creds_path.write_text(
                        json.dumps(dict(creds_json)), encoding="utf-8"
                    )
        except Exception:
            pass


_bootstrap_credentials_from_secrets()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Scopes must match engine.py exactly so both modules share the same
# token.json without triggering a re-auth flow.
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]


# ---------------------------------------------------------------------------
# Calendar service builder
# ---------------------------------------------------------------------------

def _is_cloud_environment() -> bool:
    """Return True when running on Streamlit Cloud (no interactive browser)."""
    return (
        os.environ.get("IS_STREAMLIT_CLOUD", "").lower() in ("1", "true")
        or os.environ.get("STREAMLIT_SHARING_MODE", "") != ""
        or os.environ.get("HOME", "") == "/home/appuser"
    )


def _build_calendar_service():
    """Return an authenticated Google Calendar v3 service resource.

    On Streamlit Cloud the token is bootstrapped from secrets at import
    time; only a silent refresh is ever needed (no browser popup).
    On local dev the full InstalledAppFlow runs as before.
    """
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    here = os.path.dirname(os.path.abspath(__file__))
    creds_path = os.path.join(here, "credentials.json")
    token_path = os.path.join(here, "token.json")

    creds: Credentials | None = None

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, _SCOPES)
        except ValueError as e:
            print(f"[DEBUG] token invalid: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            if _is_cloud_environment():
                raise RuntimeError(
                    "No valid Google token found on Streamlit Cloud.\n"
                    "Add your token.json content as the GOOGLE_TOKEN_JSON "
                    "secret in the Streamlit Cloud dashboard "
                    "(Settings → Secrets)."
                )

            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore

            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"Google OAuth client secrets not found at {creds_path}"
                )

            flow = InstalledAppFlow.from_client_secrets_file(creds_path, _SCOPES)
            creds = flow.run_local_server(
                host="localhost",
                port=8080,
                open_browser=True,
            )

            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

    service = build(
        "calendar",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )

    return service

# ---------------------------------------------------------------------------
# Environment / API key
# ---------------------------------------------------------------------------

from pathlib import Path

from dotenv import load_dotenv  # type: ignore

# Mirror draft_machine.py: try the project directory first, then CWD.
_HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_HERE / ".env")
load_dotenv()

_GEMINI_MODEL = "gemini-2.5-flash"

# Regex for stripping markdown code fences (```json … ``` or ``` … ```)
import re
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\n?|```$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Meeting-request parser
# ---------------------------------------------------------------------------

def parse_meeting_request(thread: dict[str, Any]) -> dict[str, Any]:
    """Use Gemini to extract meeting details from an email thread.

    Concatenates all messages in ``thread["messages"]`` into a single
    plain-text block, then asks Gemini (gemini-2.5-flash) to return a
    JSON object with the following keys:

    - ``proposed_times``    : list[str]  — ISO-8601 datetime strings
                              (e.g. ``"2026-06-25T14:00:00"``)
    - ``attendees``         : list[str]  — email addresses of all
                              participants mentioned
    - ``topic``             : str        — one-line meeting summary
    - ``duration_minutes``  : int        — meeting length; default 30 if
                              not specified in the thread

    Today's date is injected into the prompt so Gemini can resolve
    relative day names ("this Friday", "next Monday", etc.) correctly.

    Parameters
    ----------
    thread : dict
        Thread dict with ``subject`` and ``messages``.  Each message
        should have ``from``, ``date``, and ``body`` keys.

    Returns
    -------
    dict
        Parsed meeting details, or ``{"parsing_error": str}`` if anything
        goes wrong (missing API key, Gemini error, JSON parse failure).
        The function never raises.
    """
    import json as _json
    from datetime import date

    # --- 1. Build the raw thread text ----------------------------------------
    subject = thread.get("subject", "(no subject)")
    messages = thread.get("messages") or []

    thread_lines: list[str] = [f"Subject: {subject}", ""]
    for i, msg in enumerate(messages, start=1):
        thread_lines.append(f"[Message {i}]")
        thread_lines.append(f"From: {msg.get('from', '?')}")
        thread_lines.append(f"Date: {msg.get('date', '?')}")
        thread_lines.append("")
        thread_lines.append((msg.get("body") or "").strip())
        thread_lines.append("")

    thread_text = "\n".join(thread_lines).strip()

    # --- 2. Build the prompt -------------------------------------------------
    today_str = date.today().isoformat()  # e.g. "2026-06-22"

    system_instruction = (
        "You are a scheduling assistant. "
        "Extract meeting details from the email thread provided by the user. "
        "Return ONLY a valid JSON object — no prose, no markdown, no code fences. "
        "The JSON must have exactly these keys:\n"
        '  "proposed_times"   : array of ISO-8601 datetime strings '
        '(e.g. ["2026-06-25T14:00:00"]). Empty array if none found.\n'
        '  "attendees"        : array of email address strings. '
        "Include all senders and any addresses mentioned in the body.\n"
        '  "topic"            : one-line string summarising the meeting purpose.\n'
        '  "duration_minutes" : integer number of minutes. Default to 30 if not '
        "explicitly stated.\n"
        "Do not include any other keys or explanation."
    )

    user_prompt = (
        f"Today's date is {today_str}. "
        "Use it to resolve any relative day references (e.g. 'this Friday', "
        "'next Monday') into absolute ISO-8601 datetimes.\n\n"
        "--- EMAIL THREAD ---\n"
        f"{thread_text}\n"
        "--- END THREAD ---\n\n"
        "Extract the meeting details and return the JSON object now."
    )

    # --- 3. Call Gemini -------------------------------------------------------
    try:
        import google.generativeai as genai  # type: ignore
    except ImportError as exc:
        return {"parsing_error": f"google-generativeai not installed: {exc}"}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {
            "parsing_error": (
                "GEMINI_API_KEY is not set. "
                "Add it to .env or export it in your shell."
            )
        }

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=_GEMINI_MODEL,
            system_instruction=system_instruction,
        )
        response = model.generate_content(
            user_prompt,
            generation_config={
                "temperature": 0.2,   # low — we want deterministic extraction
                "top_p": 0.9,
                "max_output_tokens": 4096,
            },
        )
        print("Complete response", response)
        raw = (response.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        return {"parsing_error": f"Gemini call failed: {exc}"}

    # --- 4. Strip fences and parse JSON --------------------------------------
    cleaned = _FENCE_RE.sub("", raw).strip()
    print("Response : ", cleaned)
    try:
        parsed: dict[str, Any] = _json.loads(cleaned)
    except _json.JSONDecodeError as exc:
        return {
            "parsing_error": f"JSON parse failed: {exc}",
            "raw_response": raw,
        }

    # --- 5. Normalise / fill defaults ----------------------------------------
    if not isinstance(parsed.get("proposed_times"), list):
        parsed["proposed_times"] = []

    if not isinstance(parsed.get("attendees"), list):
        parsed["attendees"] = []

    if not isinstance(parsed.get("topic"), str):
        parsed["topic"] = subject  # fall back to email subject

    if not isinstance(parsed.get("duration_minutes"), int):
        parsed["duration_minutes"] = 30

    return parsed

# ---------------------------------------------------------------------------
# Availability helpers
# ---------------------------------------------------------------------------

def check_availability(time_min: str, time_max: str) -> bool:
    """Query the FreeBusy API to check whether the primary calendar is free.

    Parameters
    ----------
    time_min : str
        Start of the window to check, as an ISO-8601 datetime string.
        If the string has no timezone suffix (no ``+``/``-`` offset and
        no trailing ``Z``), ``Z`` (UTC) is appended automatically.
    time_max : str
        End of the window, same format rules as ``time_min``.

    Returns
    -------
    bool
        ``True``  — the calendar has no events in [time_min, time_max).
        ``False`` — the calendar is busy, or any error occurred (safe default).
    """
    try:
        from datetime import datetime, timezone

        def _ensure_tz(ts: str) -> str:
            """Append 'Z' to a naive ISO-8601 string (no offset, no Z)."""
            ts = ts.strip()
            if ts.endswith("Z"):
                return ts
            # Has an explicit UTC offset like +05:30 or -07:00 — leave as-is.
            if "+" in ts[10:] or (ts.count("-") > 2):
                return ts
            return ts + "Z"

        time_min = _ensure_tz(time_min)
        time_max = _ensure_tz(time_max)

        service = _build_calendar_service()

        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }

        result = service.freebusy().query(body=body).execute()

        # result["calendars"]["primary"]["busy"] is a list of {start, end} dicts.
        # An empty list means the slot is free.
        busy_slots = (
            result.get("calendars", {})
                  .get("primary", {})
                  .get("busy", [])
        )
        return len(busy_slots) == 0

    except Exception:  # noqa: BLE001 — any failure defaults to "busy" / unavailable
        return False


def find_free_slot(
    proposed_times: list[str],
    duration_minutes: int = 30,
) -> str | None:
    """Return the first proposed time at which the primary calendar is free.

    Iterates ``proposed_times`` in order. For each entry it:

    1. Parses the ISO-8601 string (skips malformed entries silently).
    2. Calculates ``time_max = time_min + duration_minutes``.
    3. Calls ``check_availability(time_min, time_max)``.
    4. Returns ``time_min`` on the first free slot found.

    Parameters
    ----------
    proposed_times : list[str]
        ISO-8601 datetime strings, typically from
        ``parse_meeting_request()["proposed_times"]``.
    duration_minutes : int
        Length of the meeting in minutes.  Defaults to 30.

    Returns
    -------
    str | None
        The first free ``time_min`` string (as supplied, not normalised),
        or ``None`` if no proposed time is available.
    """
    from datetime import datetime, timedelta, timezone

    for raw_time in proposed_times:
        # --- parse -------------------------------------------------------
        try:
            ts = raw_time.strip()

            # datetime.fromisoformat() in Python ≥ 3.11 handles the trailing
            # 'Z'; for 3.9/3.10 we replace it with +00:00 first.
            parse_ts = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            dt_start = datetime.fromisoformat(parse_ts)

            # Make timezone-aware (assume UTC for naive datetimes).
            if dt_start.tzinfo is None:
                dt_start = dt_start.replace(tzinfo=timezone.utc)

        except (ValueError, AttributeError):
            # Malformed string — skip gracefully.
            continue

        # --- calculate end -----------------------------------------------
        dt_end = dt_start + timedelta(minutes=max(duration_minutes, 1))

        # Format as RFC-3339 / ISO-8601 with UTC offset for the API.
        time_min = dt_start.isoformat()
        time_max = dt_end.isoformat()

        # --- check -------------------------------------------------------
        if check_availability(time_min, time_max):
            return raw_time  # return original string so callers can display it

    return None


# ---------------------------------------------------------------------------
# Event creator
# ---------------------------------------------------------------------------

def create_event(
    summary: str,
    start_time: str,
    duration_minutes: int,
    attendees: list[str],
    description: str = "",
) -> dict[str, Any]:
    """Create a Google Calendar event and send invitation emails to attendees.

    Parameters
    ----------
    summary : str
        Event title (maps to the Calendar ``summary`` field).
    start_time : str
        ISO-8601 datetime string for the event start.  Naive strings
        (no timezone offset, no trailing ``Z``) are treated as UTC.
    duration_minutes : int
        Length of the event in minutes.  Must be >= 1.
    attendees : list[str]
        Email addresses to invite.  Entries that don't contain ``@`` are
        silently filtered out before the API call.
    description : str, optional
        Free-text event description / agenda.  Defaults to ``""``.

    Returns
    -------
    dict
        The full event resource dict returned by the Calendar API, which
        includes ``id``, ``htmlLink``, ``status``, ``start``, ``end``, and
        the confirmed ``attendees`` list among other fields.

    Raises
    ------
    Exception
        Any error from the Calendar API or the OAuth flow is propagated
        to the caller (unlike the availability helpers, here a failure
        should be surfaced rather than silently swallowed).
    """
    from datetime import datetime, timedelta, timezone

    # --- 1. Parse and normalise start_time ----------------------------------
    ts = start_time.strip()
    parse_ts = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    dt_start = datetime.fromisoformat(parse_ts)

    if dt_start.tzinfo is None:
        dt_start = dt_start.replace(tzinfo=timezone.utc)

    # --- 2. Calculate end time ----------------------------------------------
    dt_end = dt_start + timedelta(minutes=max(duration_minutes, 1))

    # Calendar API expects RFC-3339; isoformat() on tz-aware datetimes
    # produces e.g. "2026-06-25T14:00:00+00:00" which the API accepts.
    start_str = dt_start.isoformat()
    end_str = dt_end.isoformat()

    # --- 3. Build event body ------------------------------------------------
    event_body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_str,
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": end_str,
            "timeZone": "UTC",
        },
    }

    # Only include attendees that look like real email addresses.
    valid_attendees = [
        {"email": addr.strip()}
        for addr in attendees
        if "@" in addr
    ]
    if valid_attendees:
        event_body["attendees"] = valid_attendees

    # --- 4. Insert via Calendar API -----------------------------------------
    service = _build_calendar_service()

    created: dict[str, Any] = (
        service.events()
        .insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="all",   # sends invitation emails to all attendees
        )
        .execute()
    )

    return created
