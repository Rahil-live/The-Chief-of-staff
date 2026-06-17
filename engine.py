"""
engine.py
=========
Chief-of-Staff email engine.

Provides `fetch_threads()` which returns the most recent N inbox threads
from Gmail via the configured Gmail MCP server.

Each thread is returned as a dict:
    {
        "thread_id": str,
        "sender":     str,
        "subject":    str,
        "snippet":    str,
        "date":       str,   # ISO-8601, e.g. "2026-06-13T12:34:56+00:00"
    }
"""
from __future__ import annotations
import socket

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
import subprocess
import re
import sys
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

# triage is imported lazily inside run_pipeline() to avoid import errors
# when the google-generativeai library version is incompatible.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESULTS = 20
GMAIL_USER_ID = "me"


# ---------------------------------------------------------------------------
# MCP JSON-RPC transport helpers
# ---------------------------------------------------------------------------

class McpClient:
    """Minimal JSON-RPC client wrapping a Gmail MCP server subprocess."""

    def __init__(self, server_path: str) -> None:
        self.server_path = server_path
        self._proc: subprocess.Popen | None = None
        self._next_id = 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    def start(self) -> None:
        """Spawn the MCP server process and perform the initialisation handshake."""
        self._proc = subprocess.Popen(
            ["node", self.server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # 1. Initialise
        self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "engine.py", "version": "1.0"},
            },
        )
        # 2. Notify the server the client is initialised.
        self._notify("notifications/initialized")

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._proc = None

    # ------------------------------------------------------------------
    # Low-level RPC primitives
    # ------------------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result portion of the response."""
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            req["params"] = params
        self._next_id += 1

        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(req) + "\n")
        self._proc.stdin.flush()

        # Read lines until we get a complete JSON object matching our id.
        target_id = req["id"]
        while True:
            line = self._proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise ConnectionError("MCP server closed the connection prematurely.")
            try:
                resp = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(resp, dict) and resp.get("id") == target_id:
                if "error" in resp:
                    raise RuntimeError(
                        f"MCP error ({method}): {resp['error']}"
                    )
                return resp.get("result", {})

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id / no response expected)."""
        req = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(req) + "\n")
        self._proc.stdin.flush()

    # ------------------------------------------------------------------
    # High-level MCP tool wrappers
    # ------------------------------------------------------------------

    def search_emails(
        self, query: str, max_results: int = 20
    ) -> list[dict[str, Any]]:
        """Call the MCP 'search_emails' tool and return the messages list."""
        result = self._call(
            "tools/call",
            {
                "name": "search_emails",
                "arguments": {
                    "query": query,
                    "maxResults": max_results,
                },
            },
        )
        raw = self._extract_text_content(result)
        messages = self._parse_search_output(raw)
        return messages

    def read_email(self, message_id: str) -> dict[str, Any]:
        """Call the MCP 'read_email' tool and return the full message object."""
        result = self._call(
            "tools/call",
            {
                "name": "read_email",
                "arguments": {"messageId": message_id},
            },
        )
        raw = self._extract_text_content(result)
        return {"id": message_id, "raw_text": raw}

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text_content(result: dict[str, Any]) -> str:
        """Pull the plain-text payload out of an MCP tool response."""
        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                return item.get("text", "")
            resource = item.get("resource", {})
            if resource.get("mimeType", "").startswith("text/"):
                return resource.get("text", "")
        return ""

    @staticmethod
    def _parse_search_output(raw: str) -> list[dict[str, Any]]:
        """
        Parse the text output of search_emails into a list of message stubs.

        The readable output looks like::

            ID: 19ed182032567688
            Subject: 2 down — and Lesson 3 is where it gets fun!
            From: Masai Live <hello@masaischool.com>
            Date: Tue, 16 Jun 2026 17:36:58 +0000

            ID: 19ed17f710709b3f
            ...
        """
        messages: list[dict[str, Any]] = []
        current: dict[str, Any] = {}

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                if current:
                    messages.append(current)
                    current = {}
                continue
            if line.startswith("ID: "):
                current["id"] = line[len("ID: "):].strip()
                current["threadId"] = current["id"]  # threads == messages here
            elif line.startswith("Subject: "):
                current["subject"] = line[len("Subject: "):].strip()
            elif line.startswith("From: "):
                current["from"] = line[len("From: "):].strip()
            elif line.startswith("Date: "):
                current["date"] = line[len("Date: "):].strip()

        if current:
            messages.append(current)

        return messages


# ---------------------------------------------------------------------------
# MCP-based fetch_threads
# ---------------------------------------------------------------------------

def _locate_mcp_server() -> str:
    """Find the Gmail MCP server index.js relative to this file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "gmail-mcp-server", "dist", "index.js"),
        os.path.join(script_dir, "..", "gmail-mcp-server", "dist", "index.js"),
    ]
    for path in candidates:
        norm = os.path.normpath(path)
        if os.path.exists(norm):
            return norm
    # Try to find via symlink / alternate locations
    raise FileNotFoundError(
        "Could not locate gmail-mcp-server/dist/index.js. "
        "Make sure the Gmail MCP server is installed."
    )


def fetch_threads(max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, str]]:
    """
    Fetch the most recent inbox threads from Gmail using the configured
    Gmail MCP server.

    Parameters
    ----------
    max_results : int
        How many inbox threads to return. Defaults to 20.

    Returns
    -------
    list[dict[str, str]]
        Each dict has keys:
            thread_id  — Gmail thread ID
            sender     — sender email address
            subject    — email subject line
            snippet    — short text snippet
            date       — ISO-8601 date string
    """
    mcp_path = _locate_mcp_server()
    threads: list[dict[str, str]] = []

    with McpClient(mcp_path) as mcp:
        # Step 1: search for inbox messages
        messages = mcp.search_emails(query="in:inbox", max_results=max_results)

        # Step 2: read each message's full details
        for msg in messages:
            mid = msg.get("id", "")
            if not mid:
                continue

            raw_response = mcp.read_email(mid)
            raw_text = raw_response.get("raw_text", "")

            # Parse the readable output into thread fields.
            thread = _parse_readable_output(raw_text, thread_id=msg.get("threadId", mid))
            if not thread["sender"]:
                # Fallback: use data from the search result
                thread["sender"] = _extract_email(msg.get("from", ""))
            if not thread["subject"]:
                thread["subject"] = msg.get("subject", "")
            threads.append(thread)

    return threads


# ---------------------------------------------------------------------------
# Readable-output parser
# ---------------------------------------------------------------------------

def _parse_readable_output(text: str, thread_id: str = "") -> dict[str, str]:
    """
    Parse the human-readable output of ``read_email`` into our thread dict.

    Example input::

        Thread ID: 19ed182032567688
        Subject: 2 down — and Lesson 3 is where it gets fun!
        From: Masai Live <hello@masaischool.com>
        To: rahil8080@gmail.com
        Date: Tue, 16 Jun 2026 17:36:58 +0000

        [Note: This email is HTML-formatted. ...]
        ...
    """
    result: dict[str, str] = {
        "thread_id": thread_id,
        "sender": "",
        "subject": "",
        "snippet": "",
        "date": "",
    }

    lines = text.splitlines()
    # Parse header block
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            # Blank line = end of headers
            break
        if line_stripped.startswith("Thread ID: "):
            result["thread_id"] = line_stripped[len("Thread ID: "):].strip()
        elif line_stripped.startswith("Subject: "):
            result["subject"] = line_stripped[len("Subject: "):].strip()
        elif line_stripped.startswith("From: "):
            raw_from = line_stripped[len("From: "):].strip()
            result["sender"] = _extract_email(raw_from)
        elif line_stripped.startswith("Date: "):
            raw_date = line_stripped[len("Date: "):].strip()
            result["date"] = _normalize_date(raw_date)

    # Build a snippet from the visible text after headers.
    result["snippet"] = _build_snippet(text)

    return result


def _extract_email(raw: str) -> str:
    """Pull out the bare email address from a 'Name <email>' string."""
    if not raw:
        return ""
    _, addr = getaddresses([raw])[0]
    return addr.lower() if addr else raw.strip().lower()


def _normalize_date(raw: str) -> str:
    """Convert a date string to ISO-8601 (UTC)."""
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return datetime.now(timezone.utc).isoformat()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _html_unescape(text: str) -> str:
    """Replace common HTML entities with their decoded characters."""
    _amp = chr(38)  # &
    _lt = chr(60)   # <
    _gt = chr(62)   # >
    _quot = chr(34) # "
    _apos = chr(39) # '
    _nbsp = chr(160) # non-breaking space

    replacements = [
        (_amp + "amp;", _amp),
        (_amp + "lt;", _lt),
        (_amp + "gt;", _gt),
        (_amp + "quot;", _quot),
        (_amp + "#39;", _apos),
        (_amp + "#x27;", _apos),
        (_amp + "#x60;", chr(96)),
        (_amp + "nbsp;", _nbsp),
        (_amp + "apos;", _apos),
    ]
    for entity, char in replacements:
        text = text.replace(entity, char)
    return text


def _build_snippet(full_text: str, max_chars: int = 200) -> str:
    """
    Extract a short plain-text snippet from the email body.

    Skips the header block, strips HTML tags, and removes tracking/note
    prefixes.
    """
    lines = full_text.splitlines()
    # Find the blank line that separates headers from body.
    body_start = 0
    for i, line in enumerate(lines):
        if not line.strip():
            body_start = i + 1
            break

    # Collect all body text, strip HTML, and collapse whitespace.
    raw_parts: list[str] = []
    for line in lines[body_start:]:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip "[Note:" lines (e.g. "HTML-formatted" notices)
        if stripped.startswith("[Note:"):
            continue
        raw_parts.append(stripped)

    body_text = " ".join(raw_parts)
    # Strip all HTML tags
    body_text = re.sub(r"<[^>]+>", "", body_text)
    # Strip tracking-image placeholders like <img ... >
    body_text = re.sub(r"<img[^>]*>", "", body_text, flags=re.IGNORECASE)
    # Decode common HTML entities
    body_text = _html_unescape(body_text)
    # Collapse whitespace
    body_text = re.sub(r"\s+", " ", body_text).strip()

    if len(body_text) <= max_chars:
        return body_text
    return body_text[:max_chars].rsplit(" ", 1)[0] + " ..."


# ---------------------------------------------------------------------------
# Pipeline: fetch -> triage
# ---------------------------------------------------------------------------

def run_pipeline(max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, str]]:
    """
    Fetch ``max_results`` inbox threads from Gmail and classify each one
    via ``triage_inbox()``. Returns the prioritized list of triaged threads.
    """
    # Lazy import to avoid dependency issues with google-generativeai
    from triage import triage_inbox, format_digest

    threads = fetch_threads(max_results=max_results)
    if not threads:
        return []
    return format_digest(triage_inbox(threads))


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    result = fetch_threads(20)
    print(f"Fetched {len(result)} thread(s).\n")
    for t in result:
        print(
            f"  {t['date']}  |  {t['sender']:<35}  |  {t['subject'][:55]}"
        )
    print()
    pprint.pprint(result)