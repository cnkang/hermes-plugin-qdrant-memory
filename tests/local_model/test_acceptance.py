"""Opt-in real CPU model acceptance; never download a model during normal CI."""

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

from qdrant_memory import standalone
from qdrant_memory.config import DEFAULTS, merge
from qdrant_memory.embedding import build_embedder
from qdrant_memory.local_embedding import LocalEmbeddingInputTooLongError
from qdrant_memory.local_model import MODEL_NAME, verify_model

pytestmark = pytest.mark.skipif(
    not os.environ.get("QDRANT_MEMORY_TEST_LOCAL_CACHE"),
    reason="Explicit prepared CPU model cache required for real-model acceptance",
)


@pytest.fixture
def prepared_profile(tmp_path, monkeypatch):
    """Initialize a private CPU profile while rejecting every network connection."""
    cache = Path(os.environ["QDRANT_MEMORY_TEST_LOCAL_CACHE"])
    verify_model(cache)

    def offline(*args, **kwargs):
        pytest.fail("CPU inference/setup must not contact any network service")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(httpx.Client, "send", offline)
    home = tmp_path / "cpu-profile"
    started = time.monotonic()
    assert (
        standalone.main(
            [
                "setup",
                "--profile",
                str(home),
                "--embedding-provider",
                "fastembed",
                "--model-cache-dir",
                str(cache),
                "--user-id",
                "test-user",
                "--agent-id",
                "test-agent",
            ]
        )
        == 0
    )
    assert time.monotonic() - started < 30
    return home, cache


def test_real_cpu_retrieval_exact_crud_restart_and_no_truncation(prepared_profile):
    """Exercise the real local model and durable core across an offline restart."""
    home, cache = prepared_profile
    texts = [
        "我喜欢简洁的中文回复。",
        "My preferred programming language is Python.",
        "项目使用 PostgreSQL 数据库。",
        "I drink green tea in the morning.",
        "我通常晚上跑步。",
        "The deployment servers run Linux.",
        "我对花生过敏。",
        "Use dark mode in the editor.",
        "每周五下午进行团队评审。",
        "I prefer to travel by bicycle.",
    ]
    queries = [
        "我喜欢什么语言和风格的回复？",
        "Which programming language do I prefer?",
        "项目的数据库是什么？",
        "What do I drink in the morning?",
        "我何时运动？",
        "Which OS hosts the deployment?",
        "我不能吃哪种食物？",
        "Which editor theme should I use?",
        "团队评审什么时候举行？",
        "How do I prefer to travel?",
    ]
    ids = []
    with standalone.open_runtime(home) as runtime:
        for text in texts:
            ids.append(standalone.call_tool(runtime, "qdrant_memory_add", {"text": text})["id"])
        for query, expected in zip(queries, ids, strict=True):
            found = standalone.call_tool(runtime, "qdrant_memory_search", {"query": query})
            assert expected in str(found)
        standalone.call_tool(
            runtime, "qdrant_memory_update", {"id": ids[0], "text": "我偏好简洁的英文回复。"}
        )
        standalone.call_tool(runtime, "qdrant_memory_delete", {"id": ids[1]})
    with standalone.open_runtime(home) as runtime:
        result = standalone.call_tool(runtime, "qdrant_memory_search", {"query": "回复语言偏好"})
        assert ids[0] in str(result)
        assert "简洁的英文" in str(result)
        result = standalone.call_tool(
            runtime, "qdrant_memory_search", {"query": "programming language"}
        )
        assert ids[1] not in str(result)
    cfg = merge(
        DEFAULTS,
        {
            "embedding": {
                "provider": "fastembed",
                "model": MODEL_NAME,
                "dimensions": 384,
                "model_cache_dir": str(cache),
            }
        },
    )
    embedder = build_embedder(None, cfg)
    try:
        with pytest.raises(LocalEmbeddingInputTooLongError):
            embedder.embed_documents(["memory " * 200])
    finally:
        embedder.close()


def test_real_cpu_official_mcp_protocol_and_process_restart(prepared_profile, tmp_path):
    """Use the official MCP client against two real offline server processes."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home, _ = prepared_profile
    guard = tmp_path / "offline-guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def offline(*args, **kwargs): raise RuntimeError('Network forbidden in CPU acceptance')\n"
        "socket.socket.connect = offline\n",
        encoding="utf-8",
    )
    allowed = {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "USERPROFILE", "TEMP", "TMP", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(PYTHONPATH=str(guard), HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")

    async def run():
        memory_id = None
        for iteration in range(2):
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "qdrant_memory.standalone", "--home", str(home), "mcp"],
                env=env,
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert {tool.name for tool in (await session.list_tools()).tools} == {
                        "qdrant_memory_search",
                        "qdrant_memory_add",
                        "qdrant_memory_update",
                        "qdrant_memory_delete",
                    }
                    if iteration == 0:
                        result = await session.call_tool(
                            "qdrant_memory_add", {"text": "CPU model persistence test"}
                        )
                        assert not result.model_dump(by_alias=True)["isError"]
                        memory_id = json.loads(result.content[0].text)["id"]
                    else:
                        result = await session.call_tool(
                            "qdrant_memory_search", {"query": "CPU model persistence test"}
                        )
                        assert (
                            not result.model_dump(by_alias=True)["isError"]
                            and memory_id in result.content[0].text
                        )
                        updated = await session.call_tool(
                            "qdrant_memory_update",
                            {"id": memory_id, "text": "Updated CPU model test"},
                        )
                        assert not updated.model_dump(by_alias=True)["isError"]
                        removed = await session.call_tool("qdrant_memory_delete", {"id": memory_id})
                        assert not removed.model_dump(by_alias=True)["isError"]
                        denied = await session.call_tool(
                            "qdrant_memory_delete", {"id": "00000000-0000-0000-0000-000000000001"}
                        )
                        assert denied.model_dump(by_alias=True)["isError"]

    asyncio.run(run())
