"""Free-only OpenRouter support for anonymous, disposable model evaluations."""

import json
import os
import re
from pathlib import Path

BASE_URL = "https://openrouter.ai/api/v1"
ROOT = Path(__file__).resolve().parents[1]


def free_model(value):
    """Reject paid, dynamic and unsafe model identifiers before any API request."""
    if (
        not isinstance(value, str)
        or len(value) > 200
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*:free", value)
    ):
        raise ValueError("Cloud tests require a fixed free model ID")
    return value


def select_chat(tier, default, *, explicit=None, environ=None):
    """Keep dispatch, tier and legacy quality selection in one precedence rule."""
    env = os.environ if environ is None else environ
    value = explicit or env.get(f"OPENROUTER_{tier.upper()}_CHAT_MODEL")
    if tier == "quality":
        value = value or env.get("OPENROUTER_QUALITY_MODEL")
    return free_model(value or default)


def resolve_models(tier, *, chat=None, embedding=None, environ=None):
    """Resolve independent model choices without requiring any optional variable."""
    if tier not in {"smoke", "quality"}:
        raise ValueError("Unsupported cloud test tier")
    env = os.environ if environ is None else environ
    definitions = json.loads((ROOT / "tests/quality/model-profiles.json").read_text())
    defaults = definitions["profiles"]["openrouter-cloud-" + tier]
    selected = free_model(
        embedding
        or env.get(f"OPENROUTER_{tier.upper()}_EMBEDDING_MODEL")
        or defaults["embedding_model"]
    )
    return {
        "chat": select_chat(tier, defaults["generation_model"], explicit=chat, environ=env),
        "embedding": selected,
        "expected_dimensions": (
            defaults["embedding_dimensions"] if selected == defaults["embedding_model"] else None
        ),
    }
