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
    records_text = _gather(session, client, type_names) if type_names else ""

    # Only block-and-return when reads WAS configured but genuinely found
    # nothing - that's still meaningful ("nothing new to read yet"). A
    # blank reads config means this agent never had a records step to
    # begin with (a pure prompt-only or action-only agent), and should go
    # straight to calling Claude with just the prompt template.
    if type_names and not records_text:
        _log(session, client, engine.module_id, "warning",
             f"Nothing to read yet for this agent ({engine.reads}) - nothing generated.")
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

    if engine.action_connector_id:
        if llm_config:
            _send_to_action_connector(session, client, engine, content)
        else:
            # Demo-mode content is fabricated placeholder text - sending
            # that to a real external system (a real Slack channel, a
            # real webhook) would be actively harmful, not just
            # unhelpful, same reasoning rest_connector.py's _demo already
            # applies to inbound syncs with no real credential.
            _log(session, client, engine.module_id, "warning",
                 f"Demo mode - not sending to the linked action connector (no AI provider key set for this account).")

    return {"generated": True}


def _send_to_action_connector(session, client, engine, content):
    """Best-effort follow-on action, not part of "did generation
    succeed" - a failed send is logged as a warning under the AGENT's
    own module_id (distinct from action_connector.send_action's own
    success/error entry under the connector's module_id, so a failed
    send doesn't read as a failed generation) and does NOT re-raise, so
    run_custom_engine still returns {"generated": True} and api_run's
    dispatch needs no new branching to handle this case.

    When the connector requires approval, this queues a PendingAction
    instead of sending - the actual send happens later, from a human
    clicking Approve (see main.py's approve endpoint), which calls
    action_connector.send_action directly rather than going back
    through here."""
    import action_connector
    from models import ActionConnector, PendingAction
    connector = session.query(ActionConnector).filter_by(id=engine.action_connector_id).first()
    if not connector:
        _log(session, client, engine.module_id, "warning",
             f"{engine.display_name}'s linked action connector no longer exists - nothing sent.")
        return
    if connector.requires_approval:
        session.add(PendingAction(
            client_id=client.id, engine_id=engine.id, engine_name=engine.display_name,
            connector_id=connector.id, connector_name=connector.display_name, content=content,
        ))
        session.commit()
        _log(session, client, engine.module_id, "success",
             f"{engine.display_name} generated a result - awaiting approval before sending to {connector.display_name}.")
        return
    try:
        action_connector.send_action(session, client, connector, content, engine_name=engine.display_name)
    except Exception:
        _log(session, client, engine.module_id, "warning",
             f"{engine.display_name} generated a result, but sending it to {connector.display_name} failed - see '{connector.module_id}' events for details.")


def _run_live(llm_config, prompt_template, records_text):
    import llm_client
    if records_text:
        full_prompt = (
            f"{prompt_template}\n\n"
            f"Use only the records below - don't invent anything not present in them.\n\n"
            f"RECORDS:\n{records_text[:20000]}"
        )
    else:
        full_prompt = prompt_template
    return llm_client.complete(
        llm_config["provider"], llm_config["api_key"], llm_config["model"],
        full_prompt, max_tokens=1500, endpoint_url=llm_config.get("endpoint_url"),
    )


def _run_demo(engine, type_names):
    reads_desc = ', '.join(type_names) if type_names else 'nothing (this agent runs from the prompt alone)'
    return (
        f"Demo mode - no AI provider key set for this account. This agent would read "
        f"{reads_desc} for this project and follow the prompt: "
        f"\"{engine.prompt_template[:200]}{'...' if len(engine.prompt_template) > 200 else ''}\". "
        f"Add a provider and API key in the header to run it for real."
    )
