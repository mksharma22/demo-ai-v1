"""Optional LLM client used ONLY to map a free-form question to a supported intent.

It never sees row data or addresses: only the user's question and the list of
supported intents. Works with any OpenAI-compatible /chat/completions endpoint
configured through environment variables. Uses the standard library so no SDK
is required.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from .settings import Settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You route questions about a finished address-matching job to one supported action.
Reply with ONLY a JSON object: {"intent": <name>, "decision": <"MATCH"|"NON-MATCH"|"REVIEW"|null>, "field": <field|null>}
Intents:
- summary: counts of MATCH / NON-MATCH / REVIEW / errors
- show_decision: list rows with a given decision
- export_decision: create a downloadable file with only rows of a given decision
- field_conflict: rows where a component differs; field is one of house_number, unit, postcode, road, city, state, country, locality, building_name, po_box
- missing_addresses: rows where AddressA or AddressB is empty
- normalization_failures: rows where libpostal normalization failed or a row errored
- top_reasons: most common reasons, optionally for one decision
- unsupported: anything else (including questions needing data the job does not store)"""


class LLMError(RuntimeError):
    pass


def classify_intent(question: str, settings: Settings) -> dict[str, Any]:
    if not settings.llm_enabled:
        raise LLMError("LLM not configured")
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question[:1000]},
        ],
    }
    req = urllib.request.Request(
        f"{settings.llm_base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.llm_api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout_s) as resp:
            payload = json.loads(resp.read().decode())
        content = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
        log.warning("LLM intent call failed (%s)", type(exc).__name__)
        raise LLMError("LLM request failed") from exc
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise LLMError("LLM reply was not JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError("LLM reply was not valid JSON") from exc
