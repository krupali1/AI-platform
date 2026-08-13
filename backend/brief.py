"""
Project-level status summary. Compiled by a plain backend algorithm
from records that are already summarized elsewhere - each meeting and
document gets its own synthesized_summary from extraction.py, and
decisions/action items/open questions are extracted from those same
records - rather than by sending everything to an LLM a second time.
The language understanding already happened once, per record; this
step is pure organization and formatting of what's already there, so
it always produces a real result with no AI provider needed and
nothing to configure.
"""
from models import Meeting, Document, Decision, ActionItem, OpenQuestion, Brief, Event


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _gather_records(session, client):
    meetings = session.query(Meeting).filter_by(client_id=client.id).order_by(Meeting.occurred_at.asc()).all()
    documents = session.query(Document).filter_by(client_id=client.id).order_by(Document.modified_at.asc()).all()
    decisions = session.query(Decision).filter_by(client_id=client.id).all()
    action_items = session.query(ActionItem).filter_by(client_id=client.id).all()
    open_questions = session.query(OpenQuestion).filter_by(client_id=client.id, status="open").all()
    return meetings, documents, decisions, action_items, open_questions


def generate_brief(session, client):
    meetings, documents, decisions, action_items, open_questions = _gather_records(session, client)
    if not meetings and not documents:
        _log(session, client, "brief-generator", "warning",
             "No meetings or documents synced yet for this project - nothing to summarize.")
        return {"generated": False}

    content = _compile_brief(client, meetings, documents, decisions, action_items, open_questions)
    session.add(Brief(client_id=client.id, content=content))
    session.commit()
    _log(session, client, "brief-generator", "success", "Compiled a new status summary")
    return {"generated": True}


def _fmt_date(dt):
    return dt.strftime("%b %-d, %Y") if dt else "undated"


def _compile_brief(client, meetings, documents, decisions, action_items, open_questions):
    open_actions = [a for a in action_items if a.status != "done"]
    sections = [
        "#### Engagement snapshot\n"
        f"{client.name} has {len(meetings)} meeting(s) and {len(documents)} document(s) synced, "
        f"with {len(decisions)} decision(s) made, {len(open_actions)} action item(s) outstanding, "
        f"and {len(open_questions)} open question(s)."
    ]

    if meetings:
        lines = [
            f"{i}. **{m.title}** ({_fmt_date(m.occurred_at)}) - "
            f"{m.synthesized_summary or m.summary or '(summary not yet generated)'}"
            for i, m in enumerate(meetings, 1)
        ]
        sections.append("#### Meetings\n" + "\n".join(lines))

    if documents:
        lines = [
            f"{i}. **{d.title}** ({_fmt_date(d.modified_at)}) - "
            f"{d.synthesized_summary or '(summary not yet generated)'}"
            for i, d in enumerate(documents, 1)
        ]
        sections.append("#### Documents\n" + "\n".join(lines))

    if decisions:
        lines = [f"{i}. {d.description}" for i, d in enumerate(decisions, 1)]
        sections.append("#### Decisions\n" + "\n".join(lines))

    if action_items:
        lines = [
            f"{i}. {a.description} (owner: {a.owner or 'unassigned'}"
            f"{', due ' + a.due_date if a.due_date else ''}, status: {a.status})"
            for i, a in enumerate(action_items, 1)
        ]
        sections.append("#### Action items\n" + "\n".join(lines))

    if open_questions:
        lines = [f"{i}. {q.description}" for i, q in enumerate(open_questions, 1)]
        sections.append("#### Open questions\n" + "\n".join(lines))

    return "\n\n".join(sections)
