"""
Phase 3 - extraction engine. Reads every Meeting and Document for the
project and, per record, does two independent things:

1. Writes a synthesized summary onto the record itself (if it doesn't
   have one yet) - what powers the repository view and the Ask layer.
2. Extracts decisions, action items, and open questions as their own
   artifacts (if this record hasn't already contributed any).

Open questions exist specifically because decisions deliberately
exclude anything unresolved - "confirm whether ABHA/ABDM linkage is in
scope" used to just get discarded. It's now captured as its own
artifact type instead, since unresolved items are often the more
actionable thing to surface in a client memory tool.

These are tracked separately from summaries so that re-running
extraction after a feature was added backfills the new thing onto
older records without re-extracting (and duplicating) facts those
records already produced under an earlier version of this pipeline. A
record with nothing new to do is skipped entirely - no wasted API call.

Same demo-mode pattern as the connectors: without an Anthropic key
available, a lightweight stand-in runs instead, so the pipeline is
demonstrable end to end without a live API call.
"""
import os
import json
import datetime

from models import Meeting, Document, Decision, ActionItem, OpenQuestion, Event

# Marks a synthesized_summary as a demo-mode placeholder rather than a real
# one, so a later live run can tell the two apart and backfill it - see
# run_extraction's needs_summary check.
_DEMO_SUMMARY_PREFIX = "Demo mode"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

EXTRACTION_PROMPT = """You are processing raw content tied to a consulting engagement. The content is either a meeting transcript or a written document.

Do four things:

1. SUMMARY - a genuine paragraph (aim for 5-8 sentences), written for someone who wasn't there and has never seen this document. Describe everything that actually matters: what topics were covered or discussed, the relevant background and context, who was involved, what was decided, what's still open or unresolved, and why any of it matters to the engagement. This is a real summary of the whole record, not a highlight reel of decisions - a reader should come away understanding what this record is actually about, even if it contains no decisions or action items at all.

2. DECISIONS - requires an explicit commitment or agreement, not just discussion. Look for language like "we will", "confirmed", "agreed", "decided", "going with". A topic that was merely discussed, debated, or left open is NOT a decision - it belongs in open_questions instead.

3. ACTION ITEMS - a concrete task with an identifiable owner. If a deadline is stated, capture it in due_date; if not, leave due_date empty rather than guessing one. If no owner is stated, use "unassigned" rather than guessing who it might be.

4. OPEN QUESTIONS - anything raised but not resolved in this content: a question asked but not answered, a dependency on someone else confirming something, a risk or concern mentioned without a decision made about it. This is deliberately the opposite of a decision - if it were resolved, it would be in decisions instead.

Do NOT include in decisions or action items:
- Hypothetical or "what if" statements
- Questions that were raised but not answered - these go in open_questions
- Personal opinions or preferences without a stated commitment
- Restated background or requirements that were already agreed elsewhere, not decided in this content

When uncertain whether something qualifies as a decision or action item, leave it out rather than guess - open_questions is the right place for genuine uncertainty, not a reason to fabricate a decision. The summary has no such uncertainty threshold - describe what's actually there, fully, even if nothing was decided.

Respond ONLY with JSON in this exact shape, no other text:
{{"summary": "...", "decisions": ["..."], "action_items": [{{"description": "...", "owner": "...", "due_date": "..."}}], "open_questions": ["..."]}}

Example:
Content: "Anju: We want the booking flow to skip re-entry for already-billed patients. Ankit: Understood, we'll design a 'Paid, ready to schedule' state for that. Bhupendra: Can we get read access to the LIS module for report status? Anju: I'll check with our IT vendor and confirm by next week. Also, should ABHA/ABDM linkage be in scope for phase one? We didn't decide."
Output: {{"summary": "This was a working session on the patient booking flow, focused specifically on how already-billed patients should be handled when they return to book a follow-up. Anju raised the concern that these patients shouldn't have to re-enter information they've already provided, and Ankit proposed introducing a distinct 'Paid, ready to schedule' state to address this - a design decision the team settled on during the call. The conversation also touched on system integration: Bhupendra asked about getting read access to the LIS (Laboratory Information System) module so that report status could be pulled directly rather than tracked manually. This wasn't resolved on the call itself - Anju agreed to check with SBAMI's IT vendor and follow up. The team also raised, without resolving, whether ABHA/ABDM linkage belongs in phase one. Overall, the session made concrete progress on the booking UX while leaving both the LIS integration and the ABHA/ABDM scope question open.", "decisions": ["Already-billed patients see a 'Paid, ready to schedule' state and skip re-entry."], "action_items": [{{"description": "Confirm HIS/LIS read access with SBAMI's IT vendor", "owner": "Anju (SBAMI)", "due_date": "next week"}}], "open_questions": ["Whether ABHA/ABDM linkage should be in scope for phase one - raised but not decided."]}}

CONTENT:
{content}
"""


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def run_extraction(session, client, llm_config=None):
    """llm_config is {"provider", "model", "api_key", "endpoint_url"} for
    the calling user's own configured provider, built by main.py's
    get_user_llm_config() - never read from a shared global, so one
    user's runs never use another user's key or provider choice.
    None means no provider configured - demo mode."""
    meetings = session.query(Meeting).filter_by(client_id=client.id).all()
    documents = session.query(Document).filter_by(client_id=client.id).all()

    sources = [("Meeting", m, m.transcript or m.summary or "") for m in meetings]
    sources += [("Document", d, d.content or "") for d in documents]

    already_facts = {(d.source_type, d.source_id) for d in session.query(Decision).filter_by(client_id=client.id).all()}
    already_facts |= {(a.source_type, a.source_id) for a in session.query(ActionItem).filter_by(client_id=client.id).all()}
    already_facts |= {(q.source_type, q.source_id) for q in session.query(OpenQuestion).filter_by(client_id=client.id).all()}

    new_summaries, new_decisions, new_actions, new_questions, failed = 0, 0, 0, 0, 0
    for source_type, obj, content in sources:
        if not content:
            continue
        # A record whose only summary is a demo-mode placeholder isn't
        # "already processed" once a real llm_config is available - it was
        # written when no key was configured (or during the auto-sync gap
        # before that was fixed to skip rather than downgrade) and would
        # otherwise be stuck with placeholder text forever, since the plain
        # not-empty check below can't tell a placeholder from a real one.
        # Only backfills on a live run - re-running in demo mode still
        # correctly leaves an existing demo summary alone.
        is_demo_summary = (obj.synthesized_summary or "").startswith(_DEMO_SUMMARY_PREFIX)
        needs_summary = not obj.synthesized_summary or (llm_config and is_demo_summary)
        needs_facts = (source_type, obj.id) not in already_facts
        if not needs_summary and not needs_facts:
            continue  # already fully processed - skip the API call entirely

        try:
            result = _extract(content, llm_config)
        except Exception as e:
            # Isolated to this one record so a single bad response (or a
            # broken key/model affecting every record) doesn't stop the
            # rest of the batch from being attempted - but logged as a
            # real error, not silently swallowed, and the record is left
            # exactly as it was so it stays eligible for retry next run
            # instead of quietly "succeeding" with a blank summary.
            failed += 1
            _log(session, client, "extraction-engine", "error",
                 f"Failed to summarize {source_type.lower()} \"{getattr(obj, 'title', obj.id)}\": {e}")
            continue

        if needs_summary:
            obj.synthesized_summary = result.get("summary", "")
            new_summaries += 1

        if needs_facts:
            for d in result.get("decisions", []):
                session.add(Decision(client_id=client.id, description=d, source_type=source_type, source_id=obj.id))
                new_decisions += 1
            for a in result.get("action_items", []):
                session.add(ActionItem(
                    client_id=client.id,
                    description=a.get("description", ""),
                    owner=a.get("owner", ""),
                    due_date=a.get("due_date", ""),
                    source_type=source_type,
                    source_id=obj.id,
                ))
                new_actions += 1
            for q in result.get("open_questions", []):
                session.add(OpenQuestion(client_id=client.id, description=q, source_type=source_type, source_id=obj.id))
                new_questions += 1

    session.commit()
    mode = f"live, {llm_config['provider']}" if llm_config else "demo"
    status = "warning" if failed else "success"
    failed_note = f", {failed} failed - see errors above" if failed else ""
    _log(session, client, "extraction-engine", status,
         f"Summarized {new_summaries} record(s), extracted {new_decisions} decision(s), "
         f"{new_actions} action item(s), {new_questions} open question(s){failed_note} ({mode})")
    return {"summaries": new_summaries, "decisions": new_decisions, "action_items": new_actions, "open_questions": new_questions, "failed": failed}


def _extract(content, llm_config=None):
    if llm_config:
        # No try/except here on purpose - a failure (bad key, wrong model
        # name, a non-JSON response, a rate limit) needs to reach
        # run_extraction()'s caller so it gets logged as a real error and
        # the record stays eligible for retry, rather than silently
        # writing an empty summary that reads as "done" while never
        # actually producing anything - which is what used to happen
        # here, with zero visibility into why a record never got a
        # summary no matter how many times it was re-synced.
        import llm_client
        text = llm_client.complete(
            llm_config["provider"], llm_config["api_key"], llm_config["model"],
            EXTRACTION_PROMPT.format(content=content[:6000]), max_tokens=1200,
            endpoint_url=llm_config.get("endpoint_url"),
        )
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        return json.loads(text)
    return _demo_extract(content)


def _demo_extract(content):
    lowered = content.lower()
    decisions, actions, questions = [], [], []
    if "uhid" in lowered:
        decisions.append("UHID logic will use mobile number + Aadhaar, consistent with SBAMI's existing system.")
    if "lis" in lowered or "his" in lowered:
        actions.append({"description": "Confirm HIS/LIS read access with SBAMI's IT vendor", "owner": "Bhupendra", "due_date": ""})
        questions.append("Whether LIS read access will be granted, and on what timeline - raised but not confirmed.")
    if "paid" in lowered or "billed" in lowered:
        decisions.append("Already-billed patients see a 'Paid, ready to schedule' state and skip re-entry.")
    if "abha" in lowered or "abdm" in lowered:
        actions.append({"description": "Confirm whether ABHA/ABDM linkage is in scope", "owner": "Anju (SBAMI)", "due_date": ""})
        questions.append("Whether ABHA/ABDM linkage is in scope for this phase - raised but not decided.")
    return {"summary": _demo_summary(content), "decisions": decisions, "action_items": actions, "open_questions": questions}


def _demo_summary(content):
    """No API key available - a real paragraph summary needs actual
    language understanding, which demo mode doesn't have. Rather than
    a near-empty keyword-triggered message that only means anything
    for SBAMI-flavored demo text, this takes the first few sentences
    of whatever content is actually there as a naive extractive
    stand-in - genuinely readable for any project, clearly labeled as
    a placeholder rather than pretending to be a synthesized summary."""
    cleaned = " ".join(content.split())
    sentences = [s.strip() for s in cleaned.replace("\n", " ").split(". ") if s.strip()]
    excerpt = ". ".join(sentences[:4])
    if excerpt and not excerpt.endswith((".", "!", "?")):
        excerpt += "."
    if not excerpt:
        return f"{_DEMO_SUMMARY_PREFIX} - no AI provider key set for this account, and no content to summarize."
    return f"{_DEMO_SUMMARY_PREFIX} (no AI provider key set for this account, so this is the opening of the raw content, not a real synthesized summary): {excerpt}"
