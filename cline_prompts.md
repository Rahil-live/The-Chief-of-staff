# The Draft Desk — Cline Prompts

We are NOT creating a new project. We already have a folder with:
- engine.py (from Sprint 1 — fetches Gmail threads)
- triage.py (from Sprint 1 — classifies by priority using Gemini)
- context_builder.py (from MS2.1 — assembles prompt context)
- draft_machine.py (from MS2.2 — calls Gemini for draft replies)
- approval_gate.py (from MS2.3 — Streamlit approve/edit/reject UI)
- tone_profile.json, past_replies.json (from MS2.1)
- credentials.json, token.json (Gmail OAuth from Sprint 1)
- .env (with GEMINI_API_KEY)
- Streamlit is ALREADY installed (from MS2.3 Thursday)

Today we ADD one file: `app.py` — a unified Streamlit app that wires the full pipeline together (reusing the approval gate pattern from Thursday's approval_gate.py).

---

## Phase 1: Streamlit Skeleton + Thread Fetching

```
In my existing project folder (which already has engine.py, triage.py, context_builder.py, draft_machine.py, approval_gate.py, tone_profile.json, past_replies.json — and Streamlit is already installed from Thursday), create a new unified Streamlit app called app.py.

Requirements:
1. Page config: title="The Draft Desk", icon="✍️", layout="wide"
2. Sidebar with: title, source selector radio (Gmail via engine.py OR Sample threads for demo), workflow navigation buttons (Inbox & Triage, Draft Generation, Approval Gate, Export Proof)
3. Session state for: threads, triaged, drafts dict, approved dict, rejected set, current_phase
4. Phase 1 "Inbox & Triage" section:
   - A "Pull & Triage Threads" button
   - When clicked with "Gmail" source: call fetch_threads() from engine.py to get real threads, then call triage_inbox() from triage.py to classify them
   - When clicked with "Sample" source: load from sample_threads.json (fallback)
   - Display threads grouped by priority (urgent, needs-reply, fyi, ignore) with expandable content
   - Show count of actionable threads (urgent + needs-reply)

Also create sample_threads.json with 5 realistic email threads as fallback. Each thread needs: id, subject, messages array (with from, date, body fields). Make them realistic work emails:
- Thread 1: Urgent production issue
- Thread 2: Manager asking for Q3 feedback
- Thread 3: Weekly standup notes (FYI)
- Thread 4: Vendor contract follow-up (needs reply)
- Thread 5: Meeting request for design review (needs reply)

Note: engine.py returns [{thread_id, sender, subject, snippet, date}]. Convert this into the format needed by our pipeline: [{id, subject, messages: [{from, date, body}]}]. The snippet becomes the body for now (full message fetch can be added later).

Streamlit and all other deps are already installed from this week — no new pip installs needed.
```

---

## Phase 2: Draft Generation

```
Add Phase 2 "Draft Generation" to app.py:

1. Get the actionable threads (urgent + needs-reply) from the triaged results
2. Add a "Generate All Drafts" button with a progress bar
3. For each actionable thread:
   - Call draft_reply(thread) from draft_machine.py (which uses context_builder internally)
   - Store the draft in st.session_state.drafts[thread_id]
4. Show each thread + draft in side-by-side columns using st.expander:
   - Left column: original thread (latest message)
   - Right column: AI-generated draft
5. Show a message pointing to "Approval Gate" when done

draft_machine.py already handles everything — it loads tone_profile.json, past_replies.json via context_builder.py, builds the prompt, calls Gemini, and returns the draft text.
```

---

## Phase 3: Approval Gate

```
Add Phase 3 "Approval Gate" to app.py. We already built this pattern in Thursday's approval_gate.py — now we integrate it into the unified app:

1. For each draft that hasn't been approved or rejected yet:
   - Show the thread subject with priority emoji
   - Two columns: left = full original thread, right = editable text_area with the draft
   - Three buttons below: Approve (saves to st.session_state.approved), Regenerate (calls draft_reply again), Reject (adds to rejected set)
   - Use st.rerun() after each action to refresh
2. Show running count: X approved, Y rejected, Z pending
3. When all drafts are reviewed, show st.balloons() and point to Export Proof

Key: the edited text in the text_area is what gets saved on Approve — so users can tweak the draft before approving.
```

---

## Phase 4: Export Proof 

```
Add Phase 4 "Export Proof" to app.py:

1. Show a preview of all approved drafts in side-by-side view (original thread | approved draft)
2. Create a generate_proof_markdown() function that outputs:
   - Title: "The Draft Desk — Proof of Work"
   - Date
   - For each approved thread: original messages quoted + draft reply in code block
3. Create a generate_proof_html() function that outputs styled HTML:
   - Dark theme (#1a1a2e background)
   - CSS grid: original thread (left, orange border) | draft (right, green border)
   - Professional enough to share on social media
4. Two download buttons: "Download Proof (Markdown)" and "Download Proof (HTML)"
```

---
