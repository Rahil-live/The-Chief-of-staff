# -*- coding: utf-8 -*-
"""
Draft Machine — MS2.2: The Draft Machine
Calls Gemini (gemini-2.5-flash) with assembled context to generate
email replies that match your tone and follow the one-ask rule.
"""

import os
from google import genai
from dotenv import load_dotenv
from context_builder import assemble_context

load_dotenv()
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

DRAFTING_RULES = """

CRITICAL DRAFTING RULES:
1. ONE-ASK RULE: Every email must have exactly ONE clear question or ONE clear response.
   Never bury multiple asks. If you need to address multiple points, make ONE the focus
   and handle the rest as statements.

2. LENGTH CONTROL:
   - Match the energy of the thread (short thread = short reply)
   - Quick replies: 2-3 sentences max
   - Substantive replies: 5 sentences max, use numbered points if needed
   - NEVER write more than the sender wrote unless adding essential info

3. NO AI FILLER:
   - Never say "I hope this email finds you well"
   - Never say "Thank you for reaching out"
   - Never say "I wanted to follow up on"
   - Never say "Please don't hesitate to"
   - Never start with "I" — start with the content

4. STRUCTURE:
   - Acknowledge their point briefly (1 line max)
   - Give your response/decision
   - End with ONE clear next step or question
"""


def draft_reply(thread, tone_path="tone_profile.json", replies_path="past_replies.json"):
    """
    Generate a draft email reply for the given thread.

    Uses:
    - Context builder (tone profile + past replies + thread history)
    - Gemini 2.5 Flash model
    - One-ask rule: every reply has exactly ONE clear question or response
    - Length control: keep replies concise and match thread energy

    Args:
        thread: dict with 'subject' and 'messages' list
        tone_path: path to tone_profile.json
        replies_path: path to past_replies.json

    Returns:
        str: the drafted reply text
    """
    context = assemble_context(thread, tone_path, replies_path)

    full_prompt = (
        context["system"]
        + "\n"
        + DRAFTING_RULES
        + "\n\n---\n\n"
        + context["user"]
        + "\n\nIMPORTANT: Output ONLY the email reply text. No subject line, no explanation, no markdown. Just the reply body as you would type it in Gmail."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=full_prompt,
    )
    return response.text.strip()


def draft_reply_with_metadata(thread, tone_path="tone_profile.json", replies_path="past_replies.json"):
    """
    Like draft_reply() but returns metadata about the generation too.
    Useful for debugging and the approval workflow (MS2.3).
    """
    context = assemble_context(thread, tone_path, replies_path)

    full_prompt = (
        context["system"]
        + "\n"
        + DRAFTING_RULES
        + "\n\n---\n\n"
        + context["user"]
        + "\n\nIMPORTANT: Output ONLY the email reply text. No subject line, no explanation, no markdown. Just the reply body as you would type it in Gmail."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=full_prompt,
    )
    draft = response.text.strip()

    return {
        "draft": draft,
        "model": "gemini-2.5-flash",
        "thread_subject": thread["subject"],
        "reply_to": thread["messages"][-1]["from"],
    }


# ============================================================
# Demo: Run on a sample thread
# ============================================================
if __name__ == "__main__":
    sample_thread = {
        "subject": "Q3 Budget Review",
        "messages": [
            {
                "from": "boss@company.com",
                "date": "June 10, 2:30 PM",
                "body": "Can you review the Q3 budget doc and share your thoughts by EOD tomorrow? Specifically the marketing allocation — I think we're overspending on paid ads."
            },
            {
                "from": "you@company.com",
                "date": "June 10, 3:15 PM",
                "body": "Got it, will review tonight. Quick question — should I loop in the marketing team lead or keep it between us?"
            },
            {
                "from": "boss@company.com",
                "date": "June 11, 9:00 AM",
                "body": "Keep it between us for now. Let me know what you find."
            }
        ]
    }

    print("=" * 60)
    print("DRAFT MACHINE — MS2.2")
    print("=" * 60)
    print("\nThread: " + sample_thread["subject"])
    print("Replying to: " + sample_thread["messages"][-1]["from"])
    print("Their message: " + sample_thread["messages"][-1]["body"])
    print("\n" + "-" * 60)
    print("GENERATING DRAFT...")
    print("-" * 60 + "\n")

    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not set!")
        print("Set it in your .env file or environment.")
        exit(1)

    result = draft_reply_with_metadata(sample_thread)

    print("DRAFT REPLY:")
    print("=" * 60)
    print(result["draft"])
    print("=" * 60)
    print("\nModel: " + result["model"])
    print("Replying to: " + result["reply_to"])
    print("Subject: " + result["thread_subject"])