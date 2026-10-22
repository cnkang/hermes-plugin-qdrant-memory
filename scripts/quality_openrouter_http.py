"""Bounded, serialized OpenRouter HTTP for synthetic model tests only."""

import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

import httpx

from qdrant_memory.embedding import validate_vectors
from qdrant_memory.quality_metrics import latency_summary


class OpenRouterFailure(RuntimeError):
    """Expose a fixed failure category without provider content or credentials."""

    def __init__(self, classification, status=None):
        """Retain only diagnostic categories and the HTTP status code."""
        self.classification, self.http_status = classification, status
        super().__init__("OpenRouter test failed: " + classification)


def classification(status, message=""):
    """Distinguish infrastructure and capability failures from semantic quality."""
    # Inspect known router reasons, but retain only fixed diagnostic categories.
    text = message.casefold() if isinstance(message, str) else ""
    if status == 404:
        if "data policy" in text or "privacy settings" in text:
            return "account_policy_restricted"
        if "support the requested parameters" in text:
            return "capability_incompatible"
    if status == 429 and any(
        marker in text for marker in ("free-models-per-day", "daily quota", "requests per day")
    ):
        return "quota_exhausted"
    return {
        400: "capability_incompatible",
        401: "authentication",
        402: "quota_exhausted",
        403: "authorization",
        404: "model_unavailable",
        429: "rate_limit",
    }.get(status, "provider_unavailable" if status >= 500 else "invalid_response")


class OpenRouterHTTP(httpx.BaseTransport):
    """Guard the existing model clients without changing Runtime or scoring rules."""

    def __init__(
        self,
        models,
        key,
        *,
        transport=None,
        clock=time.monotonic,
        sleep=time.sleep,
        max_attempts=2,
        max_requests=256,
    ):
        """Keep credentials in memory and limit all requests to the official API."""
        if not key or not isinstance(key, str) or any(key in v for v in models.values() if v):
            raise OpenRouterFailure("configuration_error")
        if not 1 <= max_attempts <= 3 or not 1 <= max_requests <= 512:
            raise OpenRouterFailure("configuration_error")
        self.models, self.key = models, key
        self.transport = transport or httpx.HTTPTransport(retries=0)
        self.clock, self.sleep = clock, sleep
        self.max_attempts, self.max_requests = max_attempts, max_requests
        self.aliases, self.clients = {}, []
        self.observed = defaultdict(Counter)
        self.attempts, self.statuses, self.errors = Counter(), Counter(), Counter()
        self.latencies = defaultdict(list)
        self.retries, self.cost_samples, self.cost = 0, 0, Decimal(0)
        self.inference_responses, self.last_started = 0, None
        self.final_failure, self.closed = None, False
        self.lock = threading.Lock()
        self.embedding_dimensions = None
        self.embedding_cache, self.embedding_cache_hits = {}, 0

    def fail(self, category, status=None):
        """Record one final failure and raise a redacted exception."""
        self.final_failure = category
        raise OpenRouterFailure(category, status) from None

    def allow_model(self, kind, aliases):
        """Bind response identities to a selected model's official catalog entries."""
        if self.models[kind] not in aliases or any(
            not isinstance(v, str)
            or self.key in v
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", v)
            for v in aliases
        ):
            self.fail("model_metadata_invalid")
        self.aliases[kind] = set(aliases)

    def client(self):
        """Return an isolated client with TLS verification and no ambient proxies."""
        client = httpx.Client(
            transport=self,
            headers={"Authorization": "Bearer " + self.key},
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        )
        self.clients.append(client)
        return client

    def close(self):
        """A host client may close while another lifecycle uses the shared transport."""

    def shutdown(self):
        """Close every client and the actual network pool once evaluation ends."""
        for client in self.clients:
            client.close()
        self.transport.close()
        self.embedding_cache.clear()
        self.key = ""
        self.closed = True

    def _prepare(self, request):
        """Reject foreign endpoints, fallback models and unvalidated model choices."""
        path = request.url.path
        metadata = {"/api/v1/models", "/api/v1/embeddings/models"} | {
            "/api/v1/models/" + v + "/endpoints" for v in self.models.values() if v
        }
        if self.closed or request.url.scheme != "https" or request.url.host != "openrouter.ai":
            self.fail("endpoint_rejected")
        if request.method == "GET" and path in metadata:
            return "metadata", request
        kind = {
            "/api/v1/chat/completions": "chat",
            "/api/v1/embeddings": "embedding",
        }.get(path)
        if request.method != "POST" or not kind:
            self.fail("endpoint_rejected")
        try:
            body = json.loads(request.read())
        except (ValueError, TypeError):
            self.fail("invalid_request")
        if (
            not isinstance(body, dict)
            or body.get("model") != self.models.get(kind)
            or kind not in self.aliases
            or "models" in body
        ):
            self.fail("model_identity_mismatch")
        provider = body.get("provider", {})
        if not isinstance(provider, dict):
            self.fail("invalid_request")
        body["provider"] = {**provider, "max_price": {"prompt": 0, "completion": 0}}
        if kind == "chat":
            body["provider"]["require_parameters"] = True
        headers = dict(request.headers)
        headers.pop("content-length", None)
        return kind, httpx.Request(
            request.method, request.url, headers=headers, json=body, extensions=request.extensions
        )

    def _backoff(self, response, attempt):
        """Honor Retry-After or stop when it exceeds this test's wait budget."""
        value = response.headers.get("Retry-After")
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return None
            if not 0 <= delay <= 30:
                return None
            return delay
        return min(2**attempt, 30)

    def _observe(self, kind, response):
        """Record only model IDs, status codes, timing and reported costs."""
        if kind == "metadata":
            return
        try:
            data = response.json()
            name = data.get("model")
            if name not in self.aliases[kind] or self.key in name:
                self.fail("model_identity_mismatch")
            self.observed[kind][name] += 1
            self.inference_responses += 1
            cost = data.get("usage", {}).get("cost")
            if cost is not None:
                amount = Decimal(str(cost))
                if not amount.is_finite() or amount != 0:
                    self.fail("paid_route")
                self.cost += amount
                self.cost_samples += 1
        except (ValueError, TypeError, AttributeError, InvalidOperation):
            self.fail("invalid_response")

    def _embedding(self, request, response):
        """Validate each vector before caching synthetic input within this run."""
        try:
            inputs = json.loads(request.read())["input"]
            data = response.json()
            rows = sorted(data["data"], key=lambda row: row["index"])
            if [row["index"] for row in rows] != list(range(len(inputs))) or any(
                type(row["index"]) is not int for row in rows
            ):
                self.fail("embedding_invalid")
            vectors = [row["embedding"] for row in rows]
            width = len(vectors[0])
            if self.embedding_dimensions is not None and width != self.embedding_dimensions:
                self.fail("dimension_mismatch")
            if width < 1 or any(isinstance(v, bool) for vector in vectors for v in vector):
                self.fail("embedding_invalid")
            validate_vectors(vectors, len(inputs), width)
        except (KeyError, ValueError, TypeError, IndexError):
            self.fail("embedding_invalid")
        self.embedding_dimensions = width
        for text, vector in zip(inputs, vectors, strict=True):
            self.embedding_cache[text] = (vector, data["model"])

    def _cached_embedding(self, request):
        """Reuse exact model inputs; observed API identities count network calls only."""
        inputs = json.loads(request.read()).get("input")
        if (
            not isinstance(inputs, list)
            or not inputs
            or not all(isinstance(t, str) for t in inputs)
        ):
            self.fail("invalid_request")
        dimensions = json.loads(request.read()).get("dimensions")
        if dimensions is not None and dimensions != self.embedding_dimensions:
            self.fail("dimension_mismatch")
        if not all(text in self.embedding_cache for text in inputs):
            return None
        self.embedding_cache_hits += len(inputs)
        return httpx.Response(
            200,
            json={
                "model": self.embedding_cache[inputs[0]][1],
                "data": [
                    {"index": i, "embedding": self.embedding_cache[text][0]}
                    for i, text in enumerate(inputs)
                ],
            },
        )

    def handle_request(self, request):
        """Serialize bounded retries across Chat, Embedding and metadata requests."""
        with self.lock:
            kind, prepared = self._prepare(request)
            if kind == "embedding":
                cached = self._cached_embedding(prepared)
                if cached is not None:
                    return cached
            for attempt in range(self.max_attempts):
                if sum(self.attempts.values()) >= self.max_requests:
                    self.fail("request_budget_exhausted")
                if self.last_started is not None:
                    self.sleep(max(0, 3.1 - (self.clock() - self.last_started)))
                started = self.last_started = self.clock()
                self.attempts[kind] += 1
                try:
                    response = self.transport.handle_request(prepared)
                    response.read()
                except httpx.TransportError:
                    self.errors["network_error"] += 1
                    if attempt + 1 == self.max_attempts:
                        self.fail("network_error")
                    self.retries += 1
                    self.sleep(2**attempt)
                    continue
                finally:
                    self.latencies[kind].append((self.clock() - started) * 1000)
                status = response.status_code
                self.statuses[str(status)] += 1
                if status == 200:
                    self._observe(kind, response)
                    if kind == "embedding":
                        self._embedding(prepared, response)
                    return response
                try:
                    message = response.json().get("error", {}).get("message", "")
                except (ValueError, TypeError, AttributeError):
                    message = ""
                category = classification(status, message)
                self.errors[category] += 1
                delay = self._backoff(response, attempt)
                response.close()
                if (
                    category == "quota_exhausted"
                    or status not in {429, 500, 502, 503, 504}
                    or attempt + 1 == self.max_attempts
                    or delay is None
                ):
                    self.fail(category, status)
                self.retries += 1
                self.sleep(delay)
            self.fail("provider_unavailable")

    def report(self):
        """Return diagnostics without request text, response bodies or credentials."""
        return {
            "api_attempts": dict(self.attempts),
            "http_statuses": dict(self.statuses),
            "observed_models": {k: dict(v) for k, v in self.observed.items()},
            "retries": self.retries,
            "error_classifications": dict(self.errors),
            "final_failure_classification": self.final_failure,
            "reported_cost_usd": str(self.cost) if self.cost_samples else None,
            "cost_verified": self.cost_samples == self.inference_responses > 0,
            "api_latency": {k: latency_summary(v) for k, v in self.latencies.items()},
            "resources_closed": self.closed,
            "embedding_dimensions": self.embedding_dimensions,
            "embedding_cache_hits": self.embedding_cache_hits,
        }
