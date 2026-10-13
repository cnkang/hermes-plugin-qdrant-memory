"""High-similarity explicit writes require neither Hermes nor an LLM."""

import asyncio
import json
import sys

import pytest

from qdrant_memory.models import Scope, content_hash, point_id

from .test_standalone import command
from .test_standalone import profile as standalone_profile

FIRST = "similarity-fixture: Cats prefer sunlight"
SECOND = "similarity-fixture: Cats avoid sunlight"


@pytest.fixture
def profile(tmp_path):
    """Reuse disposable embedded storage and the controlled embedding service."""
    yield from standalone_profile.__wrapped__(tmp_path)


def test_explicit_python_api_without_host_or_llm(profile):
    """A guarded subprocess exercises open_runtime with no extractor at cosine 1."""
    import subprocess

    home, env = profile
    command(profile, "init")
    script = f"""
from qdrant_memory.standalone import open_runtime, call_tool
from qdrant_memory.models import Scope
with open_runtime({str(home)!r}) as runtime:
    assert runtime.extractor is None
    first = call_tool(runtime, 'qdrant_memory_add', {{'text': {FIRST!r}}})
    scope = Scope(**runtime.cfg['scope'])
    assert runtime.store.search({SECOND!r}, scope)[0].score >= .97
    second = call_tool(runtime, 'qdrant_memory_add', {{'text': {SECOND!r}}})
    assert first['action'] == second['action'] == 'ADD'
    assert first['id'] != second['id']
    duplicate = call_tool(runtime, 'qdrant_memory_add', {{'text': {FIRST!r}}})
    assert duplicate == {{'action': 'SKIP', 'id': first['id']}}
    assert runtime.store.count(scope) == 2
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_explicit_cli_similarity_exact_hash_and_restart(profile):
    """Installed CLI saves both facts and preserves deterministic IDs after restart."""
    command(profile, "init")
    first = command(profile, "add", "--text", FIRST)
    second = command(profile, "add", "--text", SECOND)
    assert first["action"] == second["action"] == "ADD"
    assert first["id"] == point_id(Scope("operator", "agent"), "manual_tool", content_hash(FIRST))
    assert first["id"] != second["id"]
    assert command(profile, "add", "--text", FIRST) == {"action": "SKIP", "id": first["id"]}
    memories = command(profile, "search", "--query", FIRST)["memories"]
    assert {m["id"] for m in memories} == {first["id"], second["id"]}


def test_explicit_official_stdio_sdk_similarity(profile):
    """Actual SDK tool calls reach the host-neutral installed server."""
    import os
    import sysconfig
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command(profile, "init")
    home, env = profile
    executable = Path(sysconfig.get_path("scripts")) / (
        "qdrant-memory.exe" if os.name == "nt" else "qdrant-memory"
    )

    async def check():
        """Initialize protocol, save similar facts and inspect exact duplicates."""
        params = StdioServerParameters(
            command=str(executable),
            args=["--home", str(home), "mcp"],
            env=env,
            cwd=str(home),
        )
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                assert len((await client.list_tools()).tools) == 4
                ids = []
                for text in (FIRST, SECOND):
                    result = await client.call_tool("qdrant_memory_add", {"text": text})
                    assert not result.isError, result.content
                    value = json.loads(result.content[0].text)
                    assert value["action"] == "ADD"
                    ids.append(value["id"])
                assert len(set(ids)) == 2
                result = await client.call_tool("qdrant_memory_add", {"text": FIRST})
                assert not result.isError
                assert json.loads(result.content[0].text) == {"action": "SKIP", "id": ids[0]}

    asyncio.run(check())
    assert len(command(profile, "search", "--query", FIRST)["memories"]) == 2
