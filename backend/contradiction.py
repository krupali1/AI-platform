"""
Contradiction detector. Reads every decision on record for a project
and asks Claude whether any of them disagree with each other - exactly
the kind of thing that's easy to miss once an engagement has dozens of
meetings behind it, and easy for a model to catch by just reading
everything at once.

Regenerated fresh on every run rather than accumulated: contradictions
are a computed view over the current set of decisions, not an
append-only fact log, so the simplest correct approach is to clear the
previous results and recompute rather than try to deduplicate against
a set that hasn't changed.
"""
import os
import json

from models import Decision, Contradiction, Event

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

CONTRADICTION_PROMPT = """Below is every decision on record for a consulting engagement, each with a reference number. Find any pairs that genuinely contradict each other - not just different topics, but two decisions that can't both be true or would conflict if both were acted on.

Be conservative: only flag a pair if a reasonable person reading both would say "wait, which one is it?" Do not flag decisions that are merely about related topics, or that could both hold true (e.g. one about pricing and one about timeline are not a contradiction just because they're both business terms).

Respond ONLY with JSON in this exact shape, no other text:
{{"contradictions": [{{"a": 1, "b": 2, "why": "..."}}]}}
If there are none, return an empty list.

DECISIONS:
{decisions}
"""


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def run_contradiction_check(session, client, llm_config=None):
    decisions = session.query(Decision).filter_by(client_id=client.id).order_by(Decision.id).all()

    # Clear previous results - see module docstring on why this is
    # regenerated rather than accumulated.
    session.query(Contradiction).filter_by(client_id=client.id).delete()

    if len(decisions) < 2:
        session.commit()
        _log(session, client, "contradiction-detector", "success",
             "Fewer than two decisions on record - nothing to compare.")
        return {"found": 0}

    ref_map = {i + 1: d for i, d in enumerate(decisions)}
    decisions_text = "\n".join(f"[{i}] {d.description}" for i, d in ref_map.items())

    if llm_config:
        try:
            pairs = _check_live(llm_config, decisions_text)
            mode = f"live, {llm_config['provider']}"
        except Exception as e:
            session.commit()  # keep the clear-out even if the check itself fails
            _log(session, client, "contradiction-detector", "error", f"Contradiction check failed: {e}")
            raise
    else:
        pairs = _check_demo(ref_map)
        mode = "demo"

    found = 0
    for p in pairs:
        a_ref, b_ref = p.get("a"), p.get("b")
        if a_ref not in ref_map or b_ref not in ref_map:
            continue
        session.add(Contradiction(
            client_id=client.id,
            description=p.get("why", ""),
            decision_a_id=ref_map[a_ref].id,
            decision_b_id=ref_map[b_ref].id,
        ))
        found += 1

    session.commit()
    _log(session, client, "contradiction-detector", "success",
         f"Checked {len(decisions)} decision(s), found {found} contradiction(s) ({mode})")
    return {"found": found}


def _check_live(llm_config, decisions_text):
    import llm_client
    text = llm_client.complete(
        llm_config["provider"], llm_config["api_key"], llm_config["model"],
        CONTRADICTION_PROMPT.format(decisions=decisions_text[:15000]), max_tokens=1000,
        endpoint_url=llm_config.get("endpoint_url"),
    )
    text = text.strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    return json.loads(text).get("contradictions", [])


def _check_demo(ref_map):
    """No real language understanding available in demo mode, so this
    doesn't pretend to find real contradictions - it stays honestly
    empty rather than fabricating a plausible-looking flag."""
    return []
