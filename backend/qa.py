"""
The Ask layer. Unlike /api/query (a keyword LIKE match returning raw
records), this synthesizes an actual answer by handing everything the
project has - meeting and document summaries, decisions, action items
- to Claude in one call, and asking it to answer using only that
content, citing which records it drew from.

Scaling note: this stuffs every record for the project into context
rather than doing real retrieval first. That's a deliberate, honest
tradeoff for where this platform is right now - fine at the scale of
tens of records per project, wrong at the scale of hundreds. Moving to
real retrieval (embeddings + a vector column, per the architecture
doc) is the natural next step once a project outgrows this.
"""
import os
import json

from models import Meeting, Document, Decision, ActionItem

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

ASK_PROMPT = """You are answering a question using only the records below, from a consulting engagement's client memory. Answer directly and specifically using only what's in these records. If they don't contain enough to answer, say so plainly rather than guessing or padding with generic advice.

Cite which records support your answer using their reference codes (e.g. M1, D2).

Respond ONLY with JSON in this exact shape, no other text:
{{"answer": "...", "sources": ["M1", "D2"]}}

RECORDS:
{records}

QUESTION: {question}
"""


def ask(session, client, question, llm_config=None):
    meetings = session.query(Meeting).filter_by(client_id=client.id).order_by(Meeting.occurred_at.desc()).all()
    documents = session.query(Document).filter_by(client_id=client.id).order_by(Document.modified_at.desc()).all()
    decisions = session.query(Decision).filter_by(client_id=client.id).all()
    action_items = session.query(ActionItem).filter_by(client_id=client.id).all()

    if not meetings and not documents:
        return {"answer": "No meetings or documents have been synced for this project yet - run the connectors first.", "sources": []}

    ref_map = {}
    blocks = []
    for i, m in enumerate(meetings, start=1):
        ref = f"M{i}"
        ref_map[ref] = ("meeting", m)
        text = m.synthesized_summary or m.summary or (m.transcript or "")[:500]
        blocks.append(f"[{ref}] Meeting: \"{m.title}\"\n{text}")
    for i, d in enumerate(documents, start=1):
        ref = f"D{i}"
        ref_map[ref] = ("document", d)
        text = d.synthesized_summary or (d.content or "")[:500]
        blocks.append(f"[{ref}] Document: \"{d.title}\"\n{text}")
    if decisions:
        blocks.append("Decisions on record:\n" + "\n".join(f"- {d.description}" for d in decisions))
    if action_items:
        blocks.append("Action items on record:\n" + "\n".join(
            f"- {a.description} (owner: {a.owner or 'unassigned'}{', due ' + a.due_date if a.due_date else ''})" for a in action_items
        ))

    records_text = "\n\n".join(blocks)

    if llm_config:
        try:
            result = _ask_live(llm_config, records_text, question)
        except Exception as e:
            return {"answer": f"Couldn't reach {llm_config['provider']} to synthesize an answer: {e}", "sources": []}
    else:
        result = _demo_ask(ref_map, question)

    sources = []
    for ref in result.get("sources", []):
        entry = ref_map.get(ref)
        if not entry:
            continue
        kind, obj = entry
        sources.append({
            "ref": ref,
            "kind": kind,
            "title": obj.title,
            "source_url": obj.source_url,
        })

    return {"answer": result.get("answer", ""), "sources": sources}


def _ask_live(llm_config, records_text, question):
    import llm_client
    text = llm_client.complete(
        llm_config["provider"], llm_config["api_key"], llm_config["model"],
        ASK_PROMPT.format(records=records_text[:20000], question=question), max_tokens=1200,
        endpoint_url=llm_config.get("endpoint_url"),
    )
    text = text.strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    return json.loads(text)


def _demo_ask(ref_map, question):
    """No provider configured - falls back to a plain keyword match
    against titles and summaries, presented as an unsynthesized list
    rather than pretending to be a real answer."""
    q = question.lower()
    hits = []
    for ref, (kind, obj) in ref_map.items():
        haystack = f"{obj.title} {obj.synthesized_summary or ''}".lower()
        if any(word in haystack for word in q.split() if len(word) > 3):
            hits.append(ref)
    if not hits:
        return {
            "answer": "Demo mode - no AI provider is set for this account, so this is a plain keyword match rather than a synthesized answer, and nothing matched. Add a provider and key in the header to get real synthesized answers.",
            "sources": [],
        }
    return {
        "answer": f"Demo mode - no AI provider set, so here's a plain keyword match rather than a synthesized answer. {len(hits)} record(s) mention terms from your question; add a provider and key in the header for a real synthesized answer.",
        "sources": hits,
    }
