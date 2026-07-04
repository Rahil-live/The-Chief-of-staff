"""
context_builder.py
==================

Assembles the full prompt context for an email reply drafting agent.

The agent drafts replies in the voice of a specific person (defined in
``tone_profile.json``), using a small set of past replies
(``past_replies.json``) as few-shot examples.

Public API
----------
- ``load_tone_profile(path)``         -> dict
- ``load_past_replies(path)``         -> list
- ``format_thread_history(thread)``   -> str
- ``build_system_prompt(tone, past)`` -> str
- ``build_user_prompt(thread_str)``   -> str
- ``assemble_context(thread)``        -> {"system": str, "user": str}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_tone_profile(path: str | Path = "tone_profile.json") -> dict[str, Any]:
    """Load the tone profile JSON file and return it as a dict.

    Parameters
    ----------
    path : str | Path
        Path to the tone profile JSON file. Defaults to
        ``tone_profile.json`` in the current working directory.

    Returns
    -------
    dict
        The parsed tone profile.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_past_replies(path: str | Path = "past_replies.json") -> list[dict[str, Any]]:
    """Load the past replies JSON file and return it as a list of examples.

    Parameters
    ----------
    path : str | Path
        Path to the past replies JSON file. Defaults to
        ``past_replies.json`` in the current working directory.

    Returns
    -------
    list[dict]
        A list of past reply example dicts.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} should contain a JSON list of replies.")
    return data


# ---------------------------------------------------------------------------
# Thread formatting
# ---------------------------------------------------------------------------

def format_thread_history(thread: dict[str, Any]) -> str:
    """Format a Gmail-style email thread as a readable plain-text string.

    The ``thread`` dict is expected to have::

        {
            "subject": "Some subject",
            "messages": [
                {"from": "Alice <a@x.com>", "date": "2026-06-14 09:00", "body": "..."},
                ...
            ]
        }

    Parameters
    ----------
    thread : dict
        Thread dict with ``subject`` and ``messages``.

    Returns
    -------
    str
        A formatted, human-readable string representation of the thread.
    """
    if "subject" not in thread or "messages" not in thread:
        raise ValueError("thread must contain 'subject' and 'messages' keys")

    subject = thread["subject"]
    messages = thread["messages"]

    lines: list[str] = []
    lines.append(f"Subject: {subject}")
    lines.append(f"Messages: {len(messages)}")
    lines.append("=" * 60)
    lines.append("")

    for idx, msg in enumerate(messages, start=1):
        sender = msg.get("from", "(unknown sender)")
        date = msg.get("date", "(unknown date)")
        body = (msg.get("body") or "").strip()

        lines.append(f"[Message {idx}]")
        lines.append(f"From: {sender}")
        lines.append(f"Date: {date}")
        lines.append("-" * 40)
        lines.append(body)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _persona_block(tone_profile: dict[str, Any]) -> str:
    """Build the persona section of the system prompt."""
    name = tone_profile.get("name", "the user")
    role = tone_profile.get("role", "")
    company = tone_profile.get("company", "")
    tone = tone_profile.get("tone", "")
    voice = tone_profile.get("voice_description", "").strip()
    traits = tone_profile.get("traits", [])
    do_list = tone_profile.get("do", [])
    dont_list = tone_profile.get("dont", [])
    signature = tone_profile.get("signature", "").strip()

    parts: list[str] = []
    parts.append("You are an email reply drafting assistant.")
    parts.append(
        f"You write on behalf of {name}"
        + (f", {role}" if role else "")
        + (f" at {company}" if company else "")
        + "."
    )
    parts.append(f"The overall tone should be: {tone}.")

    if voice:
        parts.append("")
        parts.append("Voice description:")
        parts.append(voice)

    if traits:
        parts.append("")
        parts.append("Traits:")
        for t in traits:
            parts.append(f"- {t}")

    if do_list:
        parts.append("")
        parts.append("Do:")
        for item in do_list:
            parts.append(f"- {item}")

    if dont_list:
        parts.append("")
        parts.append("Don't:")
        for item in dont_list:
            parts.append(f"- {item}")

    if signature:
        parts.append("")
        parts.append("Default signature to use:")
        parts.append(signature)

    return "\n".join(parts).strip()


def _few_shot_block(past_replies: list[dict[str, Any]]) -> str:
    """Build the few-shot examples block of the system prompt."""
    if not past_replies:
        return "No past reply examples are available. Match the voice description above."

    parts: list[str] = []
    parts.append("Below are real past replies written by this person. "
                 "Match their style, structure, and tone closely.")
    parts.append("")

    for i, ex in enumerate(past_replies, start=1):
        ctx = ex.get("context", "").strip()
        subject = ex.get("incoming_subject", "").strip()
        reply = (ex.get("reply") or "").strip()

        parts.append(f"--- Example {i} ---")
        if ctx:
            parts.append(f"Context: {ctx}")
        if subject:
            parts.append(f"Incoming subject: {subject}")
        parts.append("Reply:")
        parts.append(reply)
        parts.append("")

    return "\n".join(parts).rstrip()


def build_system_prompt(
    tone_profile: dict[str, Any],
    past_replies: list[dict[str, Any]],
) -> str:
    """Build the system prompt: persona + few-shot examples.

    Parameters
    ----------
    tone_profile : dict
        Parsed tone profile (see ``load_tone_profile``).
    past_replies : list[dict]
        Parsed past replies (see ``load_past_replies``).

    Returns
    -------
    str
        The complete system prompt.
    """
    persona = _persona_block(tone_profile)
    few_shot = _few_shot_block(past_replies)

    system_prompt = (
        f"{persona}\n\n"
        "Your job: read the incoming email thread and draft a reply that "
        "sounds exactly like the person described above. Do not add commentary, "
        "preamble, or explanations - output ONLY the email body, ready to send.\n\n"
        f"{few_shot}"
    )
    return system_prompt


def build_user_prompt(thread_formatted: str) -> str:
    """Build the user message that asks the model to draft a reply.

    Parameters
    ----------
    thread_formatted : str
        Output of ``format_thread_history``.

    Returns
    -------
    str
        The user prompt string.
    """
    instructions = (
        "Here is the email thread that needs a reply. "
        "Draft a reply in the voice described in the system prompt.\n\n"
        "Requirements:\n"
        "- Match the tone, length, and structure of the past examples.\n"
        "- Be concise. Prefer short paragraphs.\n"
        "- Open with a brief, warm acknowledgment.\n"
        "- Close with a clear next step or question, and the appropriate sign-off.\n"
        "- Do not include any commentary - output ONLY the email body.\n\n"
        "--- THREAD START ---\n"
    )
    closing = "\n--- THREAD END ---\n\nDraft the reply now:"
    return f"{instructions}{thread_formatted}{closing}"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def assemble_context(thread: dict[str, Any]) -> dict[str, str]:
    """Build the full prompt context (system + user) for a given thread.

    This loads the tone profile and past replies from their default
    locations (``tone_profile.json`` and ``past_replies.json``) in the
    current working directory, then assembles both prompts.

    Parameters
    ----------
    thread : dict
        Thread dict with ``subject`` and ``messages``.

    Returns
    -------
    dict
        ``{"system": <system prompt>, "user": <user prompt>}``
    """
    tone_profile = load_tone_profile()
    past_replies = load_past_replies()
    thread_str = format_thread_history(thread)

    system_prompt = build_system_prompt(tone_profile, past_replies)
    user_prompt = build_user_prompt(thread_str)

    return {"system": system_prompt, "user": user_prompt}


# ---------------------------------------------------------------------------
# Demo / CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # A sample thread to demonstrate the end-to-end pipeline.
    sample_thread = {
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
    }

    context = assemble_context(sample_thread)

    print("=" * 70)
    print("ASSEMBLED PROMPT CONTEXT")
    print("=" * 70)
    print()
    print("--- SYSTEM PROMPT ---")
    print(context["system"])
    print()
    print("--- USER PROMPT ---")
    print(context["user"])
