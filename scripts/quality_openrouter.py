"""Free-only OpenRouter support for anonymous, disposable model evaluations."""

import hashlib
import json
import os
import re
import runpy
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from qdrant_memory.embedding import HTTPEmbeddingProvider
from qdrant_memory.owner_llm import OwnerLLM

BASE_URL = "https://openrouter.ai/api/v1"
ROOT = Path(__file__).resolve().parents[1]
HTTP = runpy.run_path(str(ROOT / "scripts/quality_openrouter_http.py"))


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


def free_pricing(pricing):
    """Require declared zero prices, including any request or auxiliary fees."""
    if not isinstance(pricing, dict) or "prompt" not in pricing:
        return False
    try:
        amounts = [Decimal(str(value)) for key, value in pricing.items() if key != "discount"]
        return bool(amounts) and all(value.is_finite() and value == 0 for value in amounts)
    except (InvalidOperation, TypeError, ValueError):
        return False


def structured_modes(parameters):
    """Recognize declared structured return formats without relaxing the schema."""
    if not isinstance(parameters, list) or not all(isinstance(p, str) for p in parameters):
        raise ValueError("Invalid model capability metadata")
    modes = set()
    if "response_format" in parameters:
        modes.add("json_object")
        if "structured_outputs" in parameters:
            modes.add("json_schema")
    if "tools" in parameters and "tool_choice" in parameters:
        modes.add("tool_call")
    return modes


class OpenRouterSession:
    """Preflight and observe the existing Chat and Embedding adapters."""

    def __init__(self, profile, *, key=None, **transport_options):
        """Use only an explicitly injected test key and the selected model profile."""
        self.profile = profile
        models = {"chat": free_model(profile["llm"]["model"])}
        if profile["embedding"]["provider"] == "openai-compatible":
            models["embedding"] = free_model(profile["embedding"]["model"])
        self.http = HTTP["OpenRouterHTTP"](
            models,
            key if key is not None else os.environ.get("OPENROUTER_API_KEY"),
            **transport_options,
        )
        self.client = self.http.client()
        self.preflight_evidence = {}

    def _metadata(self, kind):
        """Check the official catalog, active free endpoints and response aliases."""
        path = "/models" if kind == "chat" else "/embeddings/models"
        try:
            catalog = self.client.get(BASE_URL + path).json()["data"]
            selected = self.http.models[kind]
            candidates = [row for row in catalog if row.get("id") == selected]
            if len(candidates) != 1:
                self.http.fail("model_unavailable")
            row = candidates[0]
            expiry = row.get("expiration_date")
            if expiry and date.fromisoformat(expiry[:10]) <= datetime.now(UTC).date():
                self.http.fail("model_unavailable")
            if not free_pricing(row.get("pricing")):
                self.http.fail("paid_route")
            architecture = row["architecture"]
            output = "text" if kind == "chat" else "embeddings"
            if (
                "text" not in architecture["input_modalities"]
                or output not in architecture["output_modalities"]
            ):
                self.http.fail("capability_incompatible")
            if kind == "chat" and not structured_modes(row.get("supported_parameters", [])):
                self.http.fail("capability_incompatible")
            endpoint_data = self.client.get(BASE_URL + "/models/" + selected + "/endpoints").json()[
                "data"
            ]
            if endpoint_data["id"] != selected:
                self.http.fail("model_metadata_invalid")
            active = [
                e
                for e in endpoint_data["endpoints"]
                if type(e.get("status")) is int
                and e["status"] == 0
                and (e.get("uptime_last_30m") is None or e["uptime_last_30m"] > 0)
                and free_pricing(e.get("pricing"))
            ]
            if not active:
                self.http.fail("model_unavailable")
            if kind == "chat":
                modes = structured_modes(row.get("supported_parameters", [])) & set().union(
                    *(structured_modes(e.get("supported_parameters", [])) for e in active)
                )
                if not modes:
                    self.http.fail("capability_incompatible")
                mode = next(m for m in ("json_schema", "json_object", "tool_call") if m in modes)
                native_schema = mode == "json_schema"
                # Select the wire format from official capabilities, before any
                # inference. This never changes the model or the local schema.
                self.profile["llm"]["response_format_mode"] = mode
            canonical = row["canonical_slug"]
            if not canonical:
                self.http.fail("model_metadata_invalid")
            aliases = {selected, canonical} | {
                item["id"] for item in catalog if item.get("canonical_slug") == canonical
            }
            # OpenRouter lists :free as a variant of a base model. Accept its
            # response ID only after the official free entry binds that family.
            # Requests still use the exact :free slug and a zero-price ceiling.
            base = selected.removesuffix(":free")
            if canonical == base or re.fullmatch(re.escape(base) + r"-\d{8}", canonical):
                aliases.add(base)
            self.http.allow_model(kind, aliases)
            self.preflight_evidence[kind] = {
                "requested_model": selected,
                "canonical_slug": canonical,
                "allowed_response_ids": sorted(aliases),
                "metadata_sha256": hashlib.sha256(
                    json.dumps({"model": row, "endpoints": endpoint_data}, sort_keys=True).encode()
                ).hexdigest(),
                "active_free_endpoints": len(active),
            }
            if kind == "chat":
                self.preflight_evidence[kind].update(
                    response_format_mode=mode,
                    schema_enforcement="provider" if native_schema else "local",
                )
        except (KeyError, TypeError, ValueError, AttributeError):
            self.http.fail("model_metadata_invalid")

    def attach_llm(self, llm):
        """Give the existing OwnerLLM the guarded test transport."""
        llm.client.close()
        llm.client = self.http.client()

    def preflight(self):
        """Verify structured Chat and actual vector dimensions before Qdrant creation."""
        for kind in self.http.models:
            self._metadata(kind)
        # Keep vector evidence independent from the Chat capability probe. Both
        # still must pass before an Owner or temporary Qdrant collection starts.
        if "embedding" in self.http.models:
            self.client.post(
                BASE_URL + "/embeddings",
                json={
                    "model": self.http.models["embedding"],
                    "input": ["Synthetic dimension probe one.", "Synthetic dimension probe two."],
                },
            )
            actual = self.http.embedding_dimensions
            expected = self.profile.get("expected_embedding_dimensions")
            if expected is not None and actual != expected:
                self.http.fail("dimension_mismatch")
            self.profile["embedding"]["dimensions"] = actual
        llm = OwnerLLM(self.profile["llm"])
        self.attach_llm(llm)
        try:
            result = llm.complete_structured(
                instructions="Return ready=true as JSON for this synthetic API probe.",
                input=[{"type": "text", "text": "Anonymous test connection."}],
                json_schema={
                    "type": "object",
                    "properties": {"ready": {"type": "boolean"}},
                    "required": ["ready"],
                    "additionalProperties": False,
                },
            )
            if result.parsed != {"ready": True} or type(result.parsed.get("ready")) is not bool:
                self.preflight_evidence["chat_probe_error"] = "unexpected_probe_value"
                self.http.fail("capability_incompatible")
        except ValueError as error:
            self.preflight_evidence["chat_probe_error"] = {
                "Owner extraction output was truncated; retain the event": "output_truncated",
                "Owner output is not valid JSON": "invalid_json",
                "Owner output does not satisfy the JSON schema": "schema_mismatch",
                "Owner output must contain one structured function response": "invalid_tool_response",
            }.get(str(error), "invalid_structured_output")
            self.http.fail("capability_incompatible")
        except (KeyError, TypeError):
            self.http.fail("invalid_response")
        finally:
            llm.close()
        self.preflight_evidence["structured_json_verified"] = True

    def embedding_provider(self, cfg):
        """Keep the existing strict vector adapter and one isolated network pool."""
        return HTTPEmbeddingProvider(cfg, client=self.http.client(), secret_reader=lambda _: None)

    def prefetch_queries(self, cfg, texts):
        """Batch only known synthetic queries and reuse exact vectors within this run."""
        if "embedding" not in self.http.models:
            return
        options = {**cfg, "document_instruction": cfg.get("query_instruction", "")}
        provider = self.embedding_provider(options)
        try:
            provider.embed_documents(list(dict.fromkeys(texts)))
        finally:
            provider.close()

    def close(self):
        """Release test clients and clear the temporary in-memory vector cache."""
        self.http.shutdown()

    def report(self):
        """Fingerprint selected configuration without reading local user settings."""
        return {
            **self.http.report(),
            "preflight": self.preflight_evidence,
            "requested_models": self.http.models,
            "configuration_sha256": hashlib.sha256(
                json.dumps(self.profile, sort_keys=True).encode()
            ).hexdigest(),
        }
