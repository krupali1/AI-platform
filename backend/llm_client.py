"""
The abstraction every module that talks to an AI provider goes
through - extraction.py, brief.py, contradiction.py, qa.py, and
custom_engine.py all used to call Anthropic's SDK directly. Now they
all call complete() here instead, and which actual provider that
reaches is a per-user setting (User.preferred_provider), not something
baked into each module's code.

Four providers, three of which share one request shape:

- Anthropic: the Messages API, via the anthropic SDK (already a
  dependency).
- OpenAI and "custom" (any OpenAI-compatible endpoint - Groq,
  Together, a local Ollama server, anything exposing the same
  /chat/completions shape): identical request/response format, so one
  function serves both - only the URL and model string differ. This is
  deliberately the same "cover the common shape generically" move as
  the Tier A REST connector system.
- Gemini: Google's own generateContent shape, different enough
  (nested "contents"/"parts", API key as a query param rather than a
  header) that it gets its own function.

Model names are never hardcoded as the only option - every provider
takes whatever model string the user configured, since these change
fast enough that baking in "the current one" would go stale within
months. What ships as a *default suggestion* when someone first picks
a provider is a separate, much smaller concern, handled in main.py's
provider-switch endpoint, not here.
"""
import requests

DEFAULT_MAX_TOKENS = 1500


def complete(provider, api_key, model, prompt, max_tokens=DEFAULT_MAX_TOKENS, endpoint_url=None):
    if not api_key or not model:
        raise ValueError("complete() requires both an api_key and a model - callers should check for a configured key before reaching here")
    if provider == "anthropic":
        return _anthropic(api_key, model, prompt, max_tokens)
    elif provider == "openai":
        return _openai_compatible("https://api.openai.com/v1/chat/completions", api_key, model, prompt, max_tokens)
    elif provider == "gemini":
        return _gemini(api_key, model, prompt, max_tokens)
    elif provider == "custom":
        if not endpoint_url:
            raise ValueError("The custom provider needs an endpoint URL set")
        return _openai_compatible(endpoint_url, api_key, model, prompt, max_tokens)
    else:
        raise ValueError(f"unknown provider '{provider}'")


def _anthropic(api_key, model, prompt, max_tokens):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content).strip()


def _openai_compatible(endpoint_url, api_key, model, prompt, max_tokens):
    resp = requests.post(
        endpoint_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"].strip()


def _gemini(api_key, model, prompt, max_tokens):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
        timeout=60,
    )
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        # Gemini takes the API key as a query param rather than a header,
        # so the default HTTPError message (which includes the full
        # request URL) would leak it into the Event log and error toasts -
        # both visible to any project member, not just admins. Raised
        # fresh here, from the response status/body only, never the URL.
        if resp.status_code == 429:
            raise RuntimeError("Gemini rate-limited this request (429) - too many calls in a short window. Wait a bit and try again.") from None
        detail = (resp.text or "")[:300]
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {detail}") from None
    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        # Gemini returns 200 with no candidates when a response is
        # blocked by safety filters, rather than an error status -
        # worth a clear message instead of an opaque IndexError.
        reason = body.get("promptFeedback", {}).get("blockReason", "no candidates returned")
        raise RuntimeError(f"Gemini returned no result: {reason}")
    return candidates[0]["content"]["parts"][0]["text"].strip()


# Suggested defaults shown when someone first picks a provider in the
# UI - not hardcoded elsewhere, and always overridable, since these go
# stale within months of being written.
SUGGESTED_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.5",
    "gemini": "gemini-3.5-flash",
    "custom": "",
}
