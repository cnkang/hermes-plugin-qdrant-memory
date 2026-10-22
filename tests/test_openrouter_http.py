"""Free API requests retain identity, price, retry and credential boundaries."""

import json
import runpy
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    """Advance virtual time instead of delaying contract tests."""

    def __init__(self):
        """Start with no elapsed virtual time."""
        self.now, self.sleeps = 0.0, []

    def read(self):
        """Return elapsed virtual seconds."""
        return self.now

    def sleep(self, seconds):
        """Record bounded waits and advance time."""
        self.sleeps.append(seconds)
        self.now += seconds


def build(handler, **options):
    """Construct the actual transport with synthetic credentials and clock."""
    module = runpy.run_path(str(ROOT / "scripts/quality_openrouter_http.py"))
    clock = Clock()
    transport = module["OpenRouterHTTP"](
        {"chat": "fixture/chat:free", "embedding": "fixture/embed:free"},
        "synthetic-private-key",
        transport=httpx.MockTransport(handler),
        clock=clock.read,
        sleep=clock.sleep,
        **options,
    )
    transport.allow_model("chat", {"fixture/chat:free", "fixture/chat-20261022"})
    transport.allow_model("embedding", {"fixture/embed:free", "fixture/embed-20261022"})
    return transport, clock


def answer(model="fixture/chat-20261022", cost=0):
    """Return a synthetic successful chat response without conversation content."""
    return httpx.Response(200, json={"model": model, "usage": {"cost": cost}})


def post(client):
    """Send one fixed-model request using the same endpoint as OwnerLLM."""
    return client.post(
        "https://openrouter.ai/api/v1/chat/completions", json={"model": "fixture/chat:free"}
    )


@pytest.mark.parametrize("recover", [True, False])
def test_network_retries_are_bounded_and_classified(recover):
    """A transient connection failure cannot become quality failure or an endless retry."""
    attempts = []

    def handler(request):
        attempts.append(True)
        if recover and len(attempts) == 2:
            return answer()
        raise httpx.ConnectError("synthetic-private-key", request=request)

    transport, _ = build(handler)
    try:
        with transport.client() as client:
            if recover:
                assert post(client).status_code == 200
            else:
                with pytest.raises(RuntimeError) as error:
                    post(client)
                assert error.value.classification == "network_error"
        assert len(attempts) == 2
        assert "synthetic-private-key" not in json.dumps(transport.report())
    finally:
        transport.shutdown()


def test_transport_enforces_price_and_metadata_bound_identity():
    """Canonical aliases are allowed only when official metadata provided them."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer synthetic-private-key"
        return answer()

    transport, _ = build(handler)
    try:
        with transport.client() as client:
            assert post(client).status_code == 200
        assert bodies[0]["provider"]["max_price"] == {"prompt": 0, "completion": 0}
        assert bodies[0]["provider"]["require_parameters"] is True
        report = transport.report()
        assert report["observed_models"]["chat"] == {"fixture/chat-20261022": 1}
        assert report["reported_cost_usd"] == "0"
        assert "synthetic-private-key" not in json.dumps(report)
    finally:
        transport.shutdown()


@pytest.mark.parametrize("model", ["fixture/chat", "another/chat:free", "synthetic-private-key"])
def test_unknown_or_secret_response_identity_fails_closed(model):
    """Removing :free is not sufficient evidence of model equivalence."""
    transport, _ = build(lambda _: answer(model))
    try:
        with transport.client() as client, pytest.raises(RuntimeError) as error:
            post(client)
        assert error.value.classification == "model_identity_mismatch"
        assert model not in str(error.value)
        assert "synthetic-private-key" not in json.dumps(transport.report())
    finally:
        transport.shutdown()


@pytest.mark.parametrize(
    "status,classification",
    [
        (401, "authentication"),
        (403, "authorization"),
        (402, "quota_exhausted"),
        (404, "model_unavailable"),
        (400, "capability_incompatible"),
        (429, "rate_limit"),
        (502, "provider_unavailable"),
        (503, "provider_unavailable"),
    ],
)
def test_http_failures_are_classified_without_provider_body(status, classification):
    """Provider errors remain failures and never expose arbitrary server content."""
    transport, _ = build(
        lambda _: httpx.Response(status, text="synthetic-private-key"), max_attempts=1
    )
    try:
        with transport.client() as client, pytest.raises(RuntimeError) as error:
            post(client)
        assert error.value.classification == classification
        assert transport.report()["final_failure_classification"] == classification
        assert "synthetic-private-key" not in json.dumps(transport.report()) + str(error.value)
    finally:
        transport.shutdown()


@pytest.mark.parametrize(
    "message,expected",
    [
        ("No endpoints found that support the requested parameters.", "capability_incompatible"),
        ("No endpoints found matching your data policy.", "account_policy_restricted"),
        ("No endpoints found for the selected model.", "model_unavailable"),
    ],
)
def test_404_route_denials_have_safe_specific_categories(message, expected):
    """Known router reasons are enums; arbitrary provider text stays private."""
    transport, _ = build(
        lambda _: httpx.Response(
            404, json={"error": {"message": message + " synthetic-private-key"}}
        )
    )
    try:
        with transport.client() as client, pytest.raises(RuntimeError) as error:
            post(client)
        assert error.value.classification == expected
        assert transport.report()["retries"] == 0
        assert "synthetic-private-key" not in str(error.value) + json.dumps(transport.report())
        assert message not in json.dumps(transport.report())
    finally:
        transport.shutdown()


def test_retry_after_and_global_spacing_have_bounded_attempts():
    """A successful retry is visible and respects server delay without changing models."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(429, headers={"Retry-After": "7"}) if len(calls) == 1 else answer()

    transport, clock = build(handler)
    try:
        with transport.client() as client:
            post(client)
            post(client)
        assert calls == ["fixture/chat:free"] * 3
        assert 7 in clock.sleeps
        assert any(seconds >= 3.1 for seconds in clock.sleeps)
        assert transport.report()["retries"] == 1
        assert transport.report()["http_statuses"] == {"429": 1, "200": 2}
        assert transport.report()["final_failure_classification"] is None
    finally:
        transport.shutdown()


def test_request_budget_stops_before_another_api_call():
    """A total budget bounds retries and repeated lifecycle requests."""
    calls = []

    def handler(request):
        calls.append(request)
        return answer()

    transport, _ = build(handler, max_requests=1)
    try:
        with transport.client() as client:
            post(client)
            with pytest.raises(RuntimeError) as error:
                post(client)
        assert error.value.classification == "request_budget_exhausted"
        assert len(calls) == 1
    finally:
        transport.shutdown()


def test_excessive_retry_after_fails_without_retrying_early():
    """A long server backoff is respected by stopping instead of retrying early."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    transport, clock = build(handler)
    try:
        with transport.client() as client, pytest.raises(RuntimeError):
            post(client)
        assert len(calls) == 1 and not clock.sleeps
    finally:
        transport.shutdown()


def test_daily_free_quota_is_not_retried_as_a_short_rate_limit():
    """Known daily-quota responses stop without consuming more daily requests."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}
        )

    transport, _ = build(handler)
    try:
        with transport.client() as client, pytest.raises(RuntimeError) as error:
            post(client)
        assert error.value.classification == "quota_exhausted"
        assert len(calls) == 1
    finally:
        transport.shutdown()


def test_nonzero_reported_cost_is_not_certified_free():
    """A provider's nonzero charge contradicts the verified free route."""
    transport, _ = build(lambda _: answer(cost=0.01))
    try:
        with transport.client() as client, pytest.raises(RuntimeError) as error:
            post(client)
        assert error.value.classification == "paid_route"
    finally:
        transport.shutdown()


def test_credentials_cannot_follow_redirects_or_leave_openrouter():
    """Transport validation runs before any credential-bearing network request."""
    calls = []
    transport, _ = build(lambda request: calls.append(request) or answer())
    try:
        with transport.client() as client, pytest.raises(RuntimeError):
            client.get("https://untrusted.invalid/api/v1/models")
        assert not calls
    finally:
        transport.shutdown()


def test_embedding_batch_cache_preserves_indices_and_avoids_repeat_calls():
    """Batch prefetch serves identical later queries without another API call."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "fixture/embed-20261022",
                "usage": {"cost": 0},
                "data": [
                    {"index": 1, "embedding": [0, 1, 0]},
                    {"index": 0, "embedding": [1, 0, 0]},
                ],
            },
        )

    transport, _ = build(handler)
    try:
        with transport.client() as client:
            client.post(
                "https://openrouter.ai/api/v1/embeddings",
                json={
                    "model": "fixture/embed:free",
                    "input": ["one", "two"],
                },
            )
            result = client.post(
                "https://openrouter.ai/api/v1/embeddings",
                json={
                    "model": "fixture/embed:free",
                    "input": ["two"],
                },
            ).json()
        assert len(calls) == 1
        assert result["data"] == [{"index": 0, "embedding": [0, 1, 0]}]
        assert transport.report()["embedding_cache_hits"] == 1
        assert transport.report()["embedding_dimensions"] == 3
    finally:
        transport.shutdown()


@pytest.mark.parametrize("vector", [[0, 0, 0], [True, 0, 0], [float("nan"), 1, 0], []])
def test_invalid_embedding_vectors_fail_before_qdrant(vector):
    """Only finite nonzero numeric vectors can enter an isolated collection."""
    payload = {"model": "fixture/embed:free", "data": [{"index": 0, "embedding": vector}]}
    transport, _ = build(lambda _: httpx.Response(200, content=json.dumps(payload)))
    try:
        with transport.client() as client, pytest.raises(RuntimeError) as error:
            client.post(
                "https://openrouter.ai/api/v1/embeddings",
                json={
                    "model": "fixture/embed:free",
                    "input": ["synthetic"],
                },
            )
        assert error.value.classification == "embedding_invalid"
    finally:
        transport.shutdown()


def test_embedding_dimension_cannot_change_after_probe():
    """A later response cannot silently change the vector schema."""
    widths = iter([3, 2])

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": "fixture/embed:free",
                "data": [{"index": 0, "embedding": [1] * next(widths)}],
            },
        )

    transport, _ = build(handler)
    try:
        with transport.client() as client:
            client.post(
                "https://openrouter.ai/api/v1/embeddings",
                json={
                    "model": "fixture/embed:free",
                    "input": ["first"],
                },
            )
            with pytest.raises(RuntimeError) as error:
                client.post(
                    "https://openrouter.ai/api/v1/embeddings",
                    json={
                        "model": "fixture/embed:free",
                        "input": ["second"],
                    },
                )
        assert error.value.classification == "dimension_mismatch"
    finally:
        transport.shutdown()
