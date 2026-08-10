"""
The generic runner behind every config-driven agent. There is no
per-agent Python file - a PromptEngine row defines which artifact
types to read and what to ask Claude to do with them, and this module
does the actual work for all of them, the same way regardless of what
the agent is conceptually "for".

This is deliberately simpler than extraction.py: it doesn't parse
structured JSON back out, just plain text. That's what makes an agent
definable from a form instead of code - there's no output schema to
get right, just a prompt.
"""
import os

from models import Meeting, Document, Decision, ActionItem, OpenQuestion, Brief, EngineOutput, Event

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

READABLE_TYPES = {
    "Meeting": Meeting,
    "Document": Document,
    "Decision": Decision,
    "ActionItem": ActionItem,
    "OpenQuestion": OpenQuestion,
    "Brief": Brief,
}


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _split_list(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _describe(type_name, obj):
    if type_name == "Meeting":
        return f'Meeting "{obj.title}": {obj.synthesized_summary or obj.summary or ""}'
    if type_name == "Document":
        return f'Document "{obj.title}": {obj.synthesized_summary or ""}'
    if type_name == "Decision":
        return f"Decision: {obj.description}"
    if type_name == "ActionItem":
        return f"Action item: {obj.description} (owner: {obj.owner or 'unassigned'}, status: {obj.status})"
    if type_name == "OpenQuestion":
        return f"Open question: {obj.description} (status: {obj.status})"
    if type_name == "Brief":
        return f"Status brief: {obj.content}"
    return str(obj)


def _gather(session, client, type_names):
    blocks = []
    for name in type_names:
        model = READABLE_TYPES.get(name)
        if not model:
            continue
        rows = session.query(model).filter_by(client_id=client.id).all()
        for row in rows:
            blocks.append(_describe(name, row))
    return "\n".join(blocks)


def run_custom_engine(session, client, engine, llm_config=None):
    type_names = _split_list(engine.reads)
    records_text = _gather(session, client, type_names)

    if not records_text:
        _log(session, client, engine.module_id, "warning",
             f"Nothing to read yet for this agent ({engine.reads or 'no types selected'}) - nothing generated.")
        return {"generated": False}

    if llm_config:
        try:
            content = _run_live(llm_config, engine.prompt_template, records_text)
            mode = f"live, {llm_config['provider']}"
        except Exception as e:
            _log(session, client, engine.module_id, "error", f"{engine.display_name} failed: {e}")
            raise
    else:
        content = _run_demo(engine, type_names)
        mode = "demo"

    session.add(EngineOutput(client_id=client.id, engine_id=engine.id, engine_name=engine.display_name, content=content))
    session.commit()
    _log(session, client, engine.module_id, "success", f"{engine.display_name} generated a new result ({mode})")
    return {"generated": True}


def _run_live(llm_config, prompt_template, records_text):
    import llm_client
    full_prompt = (
        f"{prompt_template}\n\n"
        f"Use only the records below - don't invent anything not present in them.\n\n"
        f"RECORDS:\n{records_text[:20000]}"
    )
    return llm_client.complete(
        llm_config["provider"], llm_config["api_key"], llm_config["model"],
        full_prompt, max_tokens=1500, endpoint_url=llm_config.get("endpoint_url"),
    )


def _run_demo(engine, type_names):
    return (
        f"Demo mode - no Anthropic key set for this account. This agent would read "
        f"{', '.join(type_names) or 'nothing selected'} for this project and follow the prompt: "
        f"\"{engine.prompt_template[:200]}{'...' if len(engine.prompt_template) > 200 else ''}\". "
        f"Add an Anthropic API key in the header to run it for real."
    )
