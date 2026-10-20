"""A network outage must never count as successful authorization denial."""

import runpy
from pathlib import Path

import httpx


def test_mutation_denial_requires_an_actual_authorization_response():
    """Only explicit Owner/HTTP authorization failures qualify, including SDK groups."""
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_owner_mutations.py")
    )
    denied = namespace["is_denied"]
    request = httpx.Request("POST", "https://owner.test/mcp")
    error = httpx.HTTPStatusError(
        "synthetic", request=request, response=httpx.Response(401, request=request)
    )
    assert denied(error)
    assert denied(ExceptionGroup("SDK", [error]))
    assert denied({"error": {"type": "PermissionError"}})
    assert not denied(httpx.ReadTimeout("offline"))
    assert not denied({"memories": []})
    error = httpx.HTTPStatusError(
        "synthetic", request=request, response=httpx.Response(500, request=request)
    )
    assert not denied(error)
