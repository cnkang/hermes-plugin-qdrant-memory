"""Isolate one deterministic extraction failure without stopping other writers."""

import json

DETERMINISTIC = {"output_truncated", "output_invalid", "authentication_rejected"}


class ExtractionStageError(RuntimeError):
    """An extraction failure that persisted no write plan."""

    def __init__(self, reason, original):
        """Keep the stable reason code and the original provider exception."""
        super().__init__(f"Owner extraction failed: {reason}")
        self.reason = reason
        self.original = original

    @property
    def deterministic(self):
        """Report whether retrying the same configuration cannot succeed."""
        return self.reason in DETERMINISTIC


class EventQuarantined(RuntimeError):
    """A retained event that needs operator action but no global write stop."""

    def __init__(self, event_id, reason):
        """Identify the retained event and its stable reason code."""
        super().__init__("Lifecycle event was quarantined after an extraction failure")
        self.event_id = event_id
        self.reason = reason


def extraction_reason(exc):
    """Map a provider failure to a stable code without exposing prompt content."""
    from .owner_llm import OwnerExtractionTruncated

    if isinstance(exc, OwnerExtractionTruncated):
        return "output_truncated"
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the remote extra
        httpx = None
    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException) or isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in (401, 403):
                return "authentication_rejected"
            if status == 429:
                return "rate_limited"
            if status == 408:
                return "timeout"
            if 400 <= status < 500:
                return "output_invalid"
            return "provider_error"
        if isinstance(exc, (httpx.TransportError, ConnectionError)):
            return "provider_unreachable"
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "authentication_rejected"
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "timeout"
    if isinstance(status, int) and 400 <= status < 500:
        return "output_invalid"
    if isinstance(status, int):
        return "provider_error"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "output_invalid"
    return "extraction_failed"


def failure_record(exc):
    """Return a bounded failure record that never includes model or user text."""
    original = getattr(exc, "original", None)
    return {
        "type": type(original if original is not None else exc).__name__,
        "reason": getattr(exc, "reason", "extraction_failed"),
        "retryable": False,
    }


def record_json(exc):
    """Serialize a bounded failure record for the ledger error column."""
    return json.dumps(failure_record(exc), ensure_ascii=False)
