"""
Exception types shared by the agents, so the API can tell failures apart.

Everything used to raise a plain ValueError, and api/main.py mapped every
ValueError to HTTP 404. That meant "the model returned unparseable JSON"
and "this persona doesn't exist" arrived at the browser as the same
"Not Found", which is both wrong and impossible to debug from the UI.

Both subclass ValueError, so any existing `except ValueError` still
catches them and nothing downstream breaks.
"""


class MissingDataError(ValueError):
    """A record or table we need isn't there.

    Genuinely a 404: the persona, signal or seeded table is missing, and
    the caller asked for something that doesn't exist yet. Usually means
    load_data.py hasn't run, or ran against a different region.
    """


class AgentOutputError(ValueError):
    """Bedrock answered, but not with something we can use.

    Not a 404 — the data is fine, the model's response wasn't. Truncated
    JSON (maxTokens too low for the shape we asked for), prose wrapped
    around the object, or output that fails our schema checks. Retrying
    often works; the fix is usually the prompt or the token budget.
    """
