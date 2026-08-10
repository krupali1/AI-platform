"""
Status brief generator. Reads every meeting/document summary, every
decision, action item, and open question for a project, and asks
Claude to write a real status brief - the kind of document you'd send
someone catching up on an engagement cold. Same shape as the Ask
layer (stuff what's stored into one Claude call), just with a fixed
prompt instead of a typed question, and it writes the result as its
own artifact instead of returning it once.
"""
import os

from models import Meeting, Document, Decision, ActionItem, OpenQuestion, Brief, Event

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

BRIEF_PROMPT = """Write a status brief for this consulting engagement, using only the records below. Write it as a real document someone could send to catch a colleague up cold - not a bullet dump. Cover: what this engagement is and where it stands, what's been decided, what's still open or at risk, and what's outstanding. Plain prose in short paragraphs, a few headers if useful. Do not invent anything not present in the records.

RECORDS:
{records}
"""


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _gather_records(session, client):
    meetings = session.query(Meeting).filter_by(client_id=client.id).order_by(Meeting.occurred_at.desc()).all()
    documents = session.query(Document).filter_by(client_id=client.id).order_by(Document.modified_at.desc()).all()
    decisions = session.query(Decision).filter_by(client_id=client.id).all()
    action_items = session.query(ActionItem).filter_by(client_id=client.id).all()
    open_questions = session.query(OpenQuestion).filter_by(client_id=client.id, status="open").all()

    blocks = []
    for m in meetings:
        blocks.append(f"Meeting \"{m.title}\": {m.synthesized_summary or m.summary or ''}")
    for d in documents:
        blocks.append(f"Document \"{d.title}\": {d.synthesized_summary or ''}")
    if decisions:
        blocks.append("Decisions:\n" + "\n".join(f"- {d.description}" for d in decisions))
    if action_items:
        blocks.append("Action items:\n" + "\n".join(
            f"- {a.description} (owner: {a.owner or 'unassigned'}{', due ' + a.due_date if a.due_date else ''}, status: {a.status})"
            for a in action_items
        ))
    if open_questions:
        blocks.append("Open questions:\n" + "\n".join(f"- {q.description}" for q in open_questions))
    return meetings, documents, "\n\n".join(blocks)


def generate_brief(session, client, llm_config=None):
    meetings, documents, records_text = _gather_records(session, client)
    if not meetings and not documents:
        _log(session, client, "brief-generator", "warning",
             "No meetings or documents synced yet for this project - nothing to summarize.")
        return {"generated": False}

    if llm_config:
        try:
            content = _generate_live(llm_config, records_text)
            mode = f"live, {llm_config['provider']}"
        except Exception as e:
            _log(session, client, "brief-generator", "error", f"Brief generation failed: {e}")
            raise
    else:
        content = _generate_demo(client, meetings, documents)
        mode = "demo"

    session.add(Brief(client_id=client.id, content=content))
    session.commit()
    _log(session, client, "brief-generator", "success", f"Generated a new status brief ({mode})")
    return {"generated": True}


def _generate_live(llm_config, records_text):
    import llm_client
    return llm_client.complete(
        llm_config["provider"], llm_config["api_key"], llm_config["model"],
        BRIEF_PROMPT.format(records=records_text[:20000]), max_tokens=1500,
        endpoint_url=llm_config.get("endpoint_url"),
    )


def _generate_demo(client, meetings, documents):
    return (
        f"Demo mode - no AI provider configured for this account, so this is a plain roll-up, not a synthesized brief. "
        f"{client.name} has {len(meetings)} meeting(s) and {len(documents)} document(s) synced. "
        f"Add a provider and key in the header to generate a real status brief."
    )
