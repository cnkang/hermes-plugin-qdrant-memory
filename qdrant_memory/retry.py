"""Bounded transport retries; persisted errors never include remote response bodies."""
import random
import time


def retryable(exc):
    import httpx
    from qdrant_client.http.exceptions import ResponseHandlingException
    if isinstance(exc, ResponseHandlingException):
        return retryable(exc.source)
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    # grpc is optional in QdrantClient, including prefer_grpc deployments.
    if hasattr(exc, "code") and callable(exc.code):
        import grpc
        return exc.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
                              grpc.StatusCode.RESOURCE_EXHAUSTED}
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in {429, 502, 503, 504}


def safe_error(exc):
    return {"type": type(exc).__name__, "retryable": retryable(exc)}


def run_with_retry(fn, cfg, on_failure=None, sleeper=time.sleep):
    for attempt in range(cfg["max_attempts"]):
        try:
            return fn()
        except Exception as exc:
            if on_failure:
                on_failure(exc)
            if not retryable(exc) or attempt + 1 == cfg["max_attempts"]:
                raise
            cap = min(cfg["backoff_max_seconds"], cfg["backoff_base_seconds"] * 2 ** attempt)
            sleeper(random.uniform(0, cap))
