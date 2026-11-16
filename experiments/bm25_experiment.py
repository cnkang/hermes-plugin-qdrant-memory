#!/usr/bin/env python3
"""BM25 Hybrid Recall Validation Experiment.

This script runs controlled experiments comparing:
1. Dense-only retrieval (baseline)
2. Dense with larger candidate_k
3. BM25-only retrieval
4. Dense + BM25 / RRF hybrid retrieval

Tests both embedded and server modes with different tokenizers.
Uses Qdrant REST API directly for BM25/hybrid since Python client 1.20.0
doesn't fully support text queries yet.
"""

import hashlib
import json
import math
import os
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client import models as m

from qdrant_memory.config import DEFAULTS, merge
from qdrant_memory.embedding import build_embedder
from qdrant_memory.models import Scope, payload, point_id
from qdrant_memory.qdrant_store import QdrantStore, scope_filter

# =============================================================================
# Test Corpus Definition
# =============================================================================


class FixtureEmbedder:
    """Return deterministic vectors when CI does not have a local embedding model."""

    dimensions = 2560
    fingerprint = "bm25-fixture-v1"

    def embed_query(self, text):
        """Return a deterministic nonzero vector for one fixture query."""
        digest = hashlib.md5(text.encode()).digest()
        return [byte / 255.0 for byte in digest] + [0.0] * (self.dimensions - len(digest))

    def embed_documents(self, texts):
        """Return deterministic fixture vectors in document order."""
        return [self.embed_query(text) for text in texts]


@dataclass
class TestQuery:
    """A test query with expected relevant memory IDs and metadata."""

    query: str
    relevant_ids: list[str]
    slice: str
    difficulty: str = "normal"
    notes: str = ""


@dataclass
class MemoryRecord:
    """A memory record for the test corpus."""

    text: str
    scope: Scope
    source: str
    session_id: str = ""
    category: list[str] = field(default_factory=list)
    importance: float = 0.5
    metadata: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


# Chinese queries - no spaces, proper names, mixed writing
CHINESE_QUERIES = [
    TestQuery(
        query="北京故宫开放时间",
        relevant_ids=[],
        slice="chinese",
        notes="Chinese no-space query about Beijing Forbidden City hours",
    ),
    TestQuery(
        query="深圳地铁3号线时刻表",
        relevant_ids=[],
        slice="chinese",
        notes="Shenzhen Metro Line 3 schedule",
    ),
    TestQuery(
        query="华为Mate60Pro参数配置",
        relevant_ids=[],
        slice="chinese",
        notes="Huawei Mate 60 Pro specs with alphanumeric",
    ),
    TestQuery(
        query="张伟的生日是几月几号", relevant_ids=[], slice="chinese", notes="Personal name query"
    ),
    TestQuery(
        query="Python异步编程asyncio教程",
        relevant_ids=[],
        slice="chinese",
        notes="Mixed Chinese-English technical query",
    ),
    TestQuery(
        query="Redis集群模式部署步骤",
        relevant_ids=[],
        slice="chinese",
        notes="Technical deployment query",
    ),
    TestQuery(
        query="李白《将进酒》全文背诵",
        relevant_ids=[],
        slice="chinese",
        notes="Classical poetry query",
    ),
    TestQuery(
        query="上海迪士尼门票价格2024",
        relevant_ids=[],
        slice="chinese",
        notes="Price query with year",
    ),
    TestQuery(
        query="微信小程序云开发数据库权限",
        relevant_ids=[],
        slice="chinese",
        notes="WeChat mini-program cloud dev",
    ),
    TestQuery(
        query="Docker Compose多容器编排示例",
        relevant_ids=[],
        slice="chinese",
        notes="Docker Compose orchestration",
    ),
]

# English semantic paraphrase queries
ENGLISH_QUERIES = [
    TestQuery(
        query="How to configure Redis cluster mode",
        relevant_ids=[],
        slice="english",
        notes="Technical how-to query",
    ),
    TestQuery(
        query="Python asyncio tutorial for beginners",
        relevant_ids=[],
        slice="english",
        notes="Educational query",
    ),
    TestQuery(
        query="Docker Compose multi-container example",
        relevant_ids=[],
        slice="english",
        notes="DevOps orchestration",
    ),
    TestQuery(
        query="Beijing Forbidden City opening hours",
        relevant_ids=[],
        slice="english",
        notes="Tourist info query",
    ),
    TestQuery(
        query="Shenzhen Metro Line 3 timetable",
        relevant_ids=[],
        slice="english",
        notes="Transit schedule",
    ),
    TestQuery(
        query="Huawei Mate 60 Pro specifications",
        relevant_ids=[],
        slice="english",
        notes="Product specs",
    ),
    TestQuery(
        query="Li Bai Drinking Alone by Moonlight full text",
        relevant_ids=[],
        slice="english",
        notes="Poetry translation query",
    ),
    TestQuery(
        query="Shanghai Disneyland ticket prices 2024",
        relevant_ids=[],
        slice="english",
        notes="Price with year",
    ),
    TestQuery(
        query="WeChat mini program cloud database permissions",
        relevant_ids=[],
        slice="english",
        notes="Platform-specific dev query",
    ),
    TestQuery(
        query="Kubernetes ingress controller setup guide",
        relevant_ids=[],
        slice="english",
        notes="K8s networking",
    ),
]

# Mixed Chinese-English queries
MIXED_QUERIES = [
    TestQuery(
        query="Python async/await 语法糖原理",
        relevant_ids=[],
        slice="mixed",
        notes="Mixed technical query",
    ),
    TestQuery(
        query="React useEffect 渲染优化技巧",
        relevant_ids=[],
        slice="mixed",
        notes="Frontend optimization",
    ),
    TestQuery(
        query="Go goroutine 调度器源码分析", relevant_ids=[], slice="mixed", notes="Go internals"
    ),
    TestQuery(
        query="MySQL 索引下推 ICP 优化",
        relevant_ids=[],
        slice="mixed",
        notes="Database optimization",
    ),
    TestQuery(
        query="Kubernetes Pod 资源限制最佳实践",
        relevant_ids=[],
        slice="mixed",
        notes="K8s best practices",
    ),
    TestQuery(
        query="TensorFlow 2.x 模型部署 serving", relevant_ids=[], slice="mixed", notes="ML serving"
    ),
    TestQuery(
        query="Redis Lua 脚本原子操作", relevant_ids=[], slice="mixed", notes="Redis scripting"
    ),
    TestQuery(
        query="PostgreSQL MVCC 并发控制",
        relevant_ids=[],
        slice="mixed",
        notes="PostgreSQL internals",
    ),
    TestQuery(
        query="GraphQL N+1 问题解决方案",
        relevant_ids=[],
        slice="mixed",
        notes="GraphQL optimization",
    ),
    TestQuery(
        query="WebSocket 心跳重连机制实现",
        relevant_ids=[],
        slice="mixed",
        notes="WebSocket implementation",
    ),
]

# Code/identifier queries - exact matches matter
CODE_QUERIES = [
    TestQuery(
        query="async def fetch_data(session: aiohttp.ClientSession)",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="Exact function signature",
    ),
    TestQuery(
        query="CREATE INDEX idx_user_email ON users(email)",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="SQL DDL statement",
    ),
    TestQuery(
        query="docker run --rm -it -v $(pwd):/app python:3.11",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="Docker command with substitution",
    ),
    TestQuery(
        query="kubectl apply -f deployment.yaml --dry-run=client",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="Kubectl dry-run command",
    ),
    TestQuery(
        query="redis-cli --cluster create 127.0.0.1:7001 127.0.0.1:7002",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="Redis cluster command",
    ),
    TestQuery(
        query="SELECT * FROM users WHERE email = 'test@example.com'",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="SQL with exact string literal",
    ),
    TestQuery(
        query="npm install --save-dev typescript @types/node",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="NPM command with scoped packages",
    ),
    TestQuery(
        query="git rebase -i HEAD~5 --autosquash",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="Git interactive rebase",
    ),
    TestQuery(
        query="terraform plan -out=tfplan -var='environment=prod'",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="Terraform with variable",
    ),
    TestQuery(
        query="curl -X POST -H 'Authorization: Bearer $TOKEN'",
        relevant_ids=[],
        slice="code",
        difficulty="hard",
        notes="cURL with header and variable",
    ),
]

# Identifier queries - version numbers, error codes, paths, function names
IDENTIFIER_QUERIES = [
    TestQuery(
        query="Python 3.11.9 release notes",
        relevant_ids=[],
        slice="identifiers",
        notes="Version number query",
    ),
    TestQuery(
        query="Error code ECONNREFUSED 111",
        relevant_ids=[],
        slice="identifiers",
        notes="Error code with number",
    ),
    TestQuery(
        query="/usr/local/bin/python3.11",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="Full path query",
    ),
    TestQuery(
        query="function handleUserLogin(req, res)",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="Function name with params",
    ),
    TestQuery(
        query="class UserServiceImpl implements UserService",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="Class declaration",
    ),
    TestQuery(
        query="CONST MAX_RETRY_ATTEMPTS = 3",
        relevant_ids=[],
        slice="identifiers",
        notes="Constant definition",
    ),
    TestQuery(
        query="interface ApiResponse<T> { data: T }",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="Generic TypeScript interface",
    ),
    TestQuery(
        query="import { useState, useEffect } from 'react'",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="ES6 import with named exports",
    ),
    TestQuery(
        query="export default function App() { return <div>Hello</div> }",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="Default export component",
    ),
    TestQuery(
        query="pytest -xvs tests/test_auth.py::test_login",
        relevant_ids=[],
        slice="identifiers",
        difficulty="hard",
        notes="Pytest node ID",
    ),
]

# Negation / stale fact queries - should NOT retrieve certain memories
NEGATION_STALE_QUERIES = [
    TestQuery(
        query="Python 2.7 仍然受支持",
        relevant_ids=[],
        slice="stale_fact",
        notes="Stale fact - Python 2.7 is EOL",
    ),
    TestQuery(
        query="Vue 2 还是最新版本",
        relevant_ids=[],
        slice="stale_fact",
        notes="Stale fact - Vue 3 is current",
    ),
    TestQuery(
        query="Docker 不支持 Apple Silicon",
        relevant_ids=[],
        slice="stale_fact",
        notes="Stale fact - Docker supports M1/M2/M3",
    ),
    TestQuery(
        query="不使用 TypeScript 的项目",
        relevant_ids=[],
        slice="negation",
        notes="Negative query - projects NOT using TypeScript",
    ),
    TestQuery(
        query="已废弃的 React class 组件写法",
        relevant_ids=[],
        slice="stale_fact",
        notes="Deprecated pattern",
    ),
    TestQuery(
        query="不再维护的 webpack 4 配置",
        relevant_ids=[],
        slice="stale_fact",
        notes="Unmaintained webpack 4",
    ),
    TestQuery(
        query="已移除的 Python asyncio.coroutine 装饰器",
        relevant_ids=[],
        slice="stale_fact",
        notes="Removed decorator",
    ),
    TestQuery(
        query="不支持的旧版 Node.js 14",
        relevant_ids=[],
        slice="stale_fact",
        notes="Unsupported Node version",
    ),
    TestQuery(
        query="已弃用的 Kubernetes API extensions/v1beta1",
        relevant_ids=[],
        slice="stale_fact",
        notes="Deprecated K8s API",
    ),
    TestQuery(
        query="不再推荐的 Redux createStore 用法",
        relevant_ids=[],
        slice="stale_fact",
        notes="Deprecated Redux pattern",
    ),
]

ALL_QUERIES = (
    CHINESE_QUERIES
    + ENGLISH_QUERIES
    + MIXED_QUERIES
    + CODE_QUERIES
    + IDENTIFIER_QUERIES
    + NEGATION_STALE_QUERIES
)


# =============================================================================
# Test Corpus Generation
# =============================================================================


def generate_test_corpus(scope: Scope) -> list[MemoryRecord]:
    """Generate a diverse test corpus covering all query slices."""
    memories = []

    chinese_content = [
        "北京故宫博物院开放时间：旺季（4月1日-10月31日）8:30-17:00，淡季（11月1日-次年3月31日）8:30-16:30，周一闭馆（法定节假日除外）。",
        "深圳地铁3号线（龙岗线）首末车时刻表：益田站首车06:30末车23:00，双龙站首车06:30末车23:30，全程约55分钟。",
        "华为Mate 60 Pro搭载麒麟9000S芯片，6.82英寸OLED屏幕，5000mAh电池，支持88W有线快充、50W无线快充，后置5000万主摄+1200万超广角+4800万潜望长焦。",
        "张伟生日是1990年5月15日，籍贯河北石家庄，现就职于北京某科技公司担任高级工程师。",
        "Python异步编程核心是asyncio库，async/await语法糖让异步代码看起来像同步代码。事件循环event_loop是核心，使用asyncio.run()启动协程。",
        "Redis集群模式部署：至少需要6个节点（3主3从），使用redis-cli --cluster create创建集群，槽位分配16384个slots均匀分布到主节点。",
        "李白《将进酒》全文：君不见黄河之水天上来，奔流到海不复回。君不见高堂明镜悲白发，朝如青丝暮成雪。人生得意须尽欢，莫使金樽空对月。",
        "上海迪士尼乐园2024年门票价格：常规日399元，特别常规日575元，高峰日665元，特别高峰日799元。儿童/老人/残疾人享受优惠价。",
        "微信小程序云开发数据库权限：仅创建者可读写、所有用户可读仅创建者可读写、管理员可读写、仅管理员可读写。需在云函数中配置权限规则。",
        "Docker Compose多容器编排示例：version: '3.8', services: web: build: ., ports: - '8000:8000', depends_on: - db; db: image: postgres:15, environment: POSTGRES_PASSWORD=secret。",
    ]

    english_content = [
        "Beijing Forbidden City opening hours: Peak season (Apr 1 - Oct 31) 8:30-17:00, off-season (Nov 1 - Mar 31) 8:30-16:30. Closed Mondays except public holidays.",
        "Shenzhen Metro Line 3 (Longgang Line) timetable: Yitian station first train 06:30 last 23:00, Shuanglong station first 06:30 last 23:30. Full journey ~55 minutes.",
        "Huawei Mate 60 Pro features Kirin 9000S chip, 6.82-inch OLED display, 5000mAh battery, supports 88W wired and 50W wireless charging. Triple camera: 50MP main + 12MP ultra-wide + 48MP periscope telephoto.",
        "Zhang Wei's birthday is May 15, 1990. From Shijiazhuang, Hebei. Currently works as senior engineer at a Beijing tech company.",
        "Python async programming centers on asyncio library. async/await syntax makes async code read like synchronous. Event loop is core, use asyncio.run() to start coroutines.",
        "Redis cluster deployment: minimum 6 nodes (3 masters + 3 replicas). Create with redis-cli --cluster create. 16384 slots distributed evenly across master nodes.",
        "Li Bai 'Drinking Alone by Moonlight' full text: 'Do you not see Yellow River waters come from heaven, rushing to sea never returning...' (translated).",
        "Shanghai Disneyland 2024 ticket prices: Regular day 399 CNY, Special regular 575, Peak 665, Special peak 799. Discounts for children/seniors/disabled.",
        "WeChat Mini Program cloud database permissions: creator read-write only, all users read-only creator write, admin read-write, admin only. Configure in cloud functions.",
        "Docker Compose multi-container example: version: '3.8', services: web: build: ., ports: - '8000:8000', depends_on: - db; db: image: postgres:15, env: POSTGRES_PASSWORD=secret.",
    ]

    mixed_content = [
        "Python async/await 语法糖原理：async 函数返回 coroutine 对象，await 暂停协程直到 awaitable 完成，事件循环调度 ready 协程。底层是生成器和 yield from 机制。",
        "React useEffect 渲染优化技巧：使用 useMemo/useCallback 缓存依赖，拆分 effect 粒度，避免不必要的重渲染。依赖数组引用稳定性是关键。",
        "Go goroutine 调度器源码分析：G-M-P 模型，G=goroutine，M=OS线程，P=逻辑处理器。工作窃取算法平衡负载，系统监控防止饥饿。",
        "MySQL 索引下推 ICP 优化：在存储引擎层过滤 WHERE 条件，减少回表次数。EXPLAIN 输出 Using index condition 表示生效。",
        "Kubernetes Pod 资源限制最佳实践：requests 设置为典型用量，limits 设置为峰值上限。QoS 类别：Guaranteed > Burstable > BestEffort。避免 limit 过大导致资源浪费。",
        "TensorFlow 2.x 模型部署 serving：SavedModel 格式，TensorFlow Serving gRPC/REST API。支持批处理、模型版本管理、A/B 测试。",
        "Redis Lua 脚本原子操作：EVAL/EVALSHA 执行脚本，脚本内所有命令原子执行。KEYS 和 ARGV 传参。避免长脚本阻塞事件循环。",
        "PostgreSQL MVCC 并发控制：多版本并发控制，读不阻塞写，写不阻塞读。事务隔离级别 Read Committed 默认，Serializable 最强。VACUUM 清理死元组。",
        "GraphQL N+1 问题解决方案：DataLoader 批量加载合并同一 tick 内的请求。或使用 @defer/@stream 指令。解析器层面批量查询数据库。",
        "WebSocket 心跳重连机制实现：客户端定时发送 ping，服务端回复 pong。超时未收到 pong 触发重连。指数退避 + 抖动防止惊群。",
    ]

    code_content = [
        "async def fetch_data(session: aiohttp.ClientSession, url: str) -> dict:\n    async with session.get(url) as resp:\n        return await resp.json()",
        "CREATE INDEX idx_user_email ON users(email) INCLUDE (name, created_at);",
        "docker run --rm -it -v $(pwd):/app -w /app python:3.11-slim bash",
        "kubectl apply -f deployment.yaml --dry-run=client -o yaml",
        "redis-cli --cluster create 127.0.0.1:7001 127.0.0.1:7002 127.0.0.1:7003 --cluster-replicas 1",
        "SELECT id, name, email FROM users WHERE email = 'test@example.com' AND active = true;",
        "npm install --save-dev typescript@5.3.3 @types/node@20.10.6 eslint@8.56.0",
        "git rebase -i HEAD~5 --autosquash --rebase-merges",
        "terraform plan -out=tfplan -var='environment=prod' -var='region=us-east-1'",
        "curl -X POST https://api.example.com/v1/users -H 'Authorization: Bearer $TOKEN' -H 'Content-Type: application/json' -d '{\"name\":\"test\"}'",
    ]

    identifier_content = [
        "Python 3.11.9 released 2024-04-02. Security fixes for CVE-2024-XXXX. Performance improvements in asyncio and CPython startup time.",
        "Error code ECONNREFUSED (111): Connection refused. Typically means target port not listening. Check firewall, service status, bind address.",
        "/usr/local/bin/python3.11 - Python 3.11 interpreter installed via Homebrew on macOS. Symlinked from /opt/homebrew/bin/python3.11 on Apple Silicon.",
        "function handleUserLogin(req, res) { const { email, password } = req.body; const user = await User.findOne({ email }); if (!user || !await bcrypt.compare(password, user.hash)) return res.status(401).json({ error: 'Invalid credentials' }); req.session.userId = user.id; res.json({ success: true }); }",
        "class UserServiceImpl implements UserService { private final UserRepository repo; public UserServiceImpl(UserRepository repo) { this.repo = repo; } @Override public User findByEmail(String email) { return repo.findByEmail(email).orElseThrow(() -> new UserNotFoundException(email)); } }",
        "CONST MAX_RETRY_ATTEMPTS = 3; // Maximum retry attempts for transient failures",
        "interface ApiResponse<T> { data: T; status: number; message?: string; timestamp: string; }",
        "import { useState, useEffect, useCallback, useMemo } from 'react'; import type { Dispatch, SetStateAction } from 'react';",
        "export default function App() { const [count, setCount] = useState(0); return <div className='counter'>Count: {count} <button onClick={() => setCount(c => c + 1)}>Increment</button></div>; }",
        "pytest -xvs tests/test_auth.py::test_login -k 'not slow' --tb=short --cov=auth --cov-report=term-missing",
    ]

    stale_content = [
        "Python 2.7 仍然受支持，官方提供安全更新直到 2025 年。",
        "Vue 2 还是最新版本，Vue 3 还在测试阶段。",
        "Docker 不支持 Apple Silicon M1/M2 芯片，需要 Rosetta 2 转译运行。",
        "React class 组件是推荐写法，Hooks 还不稳定。",
        "webpack 4 是最新稳定版，webpack 5 还在 beta。",
        "Python asyncio.coroutine 装饰器是标准写法，async/await 只是语法糖。",
        "Node.js 14 是长期支持 LTS 版本，推荐生产环境使用。",
        "Kubernetes API extensions/v1beta1 稳定可用，生产环境推荐。",
        "Redux createStore 是标准创建 store 方式，configureStore 只是封装。",
        "TypeScript 可选链 ?. 和空值合并 ?? 还在提案阶段。",
    ]

    all_content = [
        (chinese_content, "chinese"),
        (english_content, "english"),
        (mixed_content, "mixed"),
        (code_content, "code"),
        (identifier_content, "identifiers"),
        (stale_content, "stale_fact"),
    ]

    for content_list, category in all_content:
        for i, text in enumerate(content_list):
            mem_id = point_id(scope, category, f"{category}_{i}")
            memories.append(
                MemoryRecord(
                    id=mem_id,
                    text=text,
                    scope=scope,
                    source="experiment_corpus",
                    category=[category],
                    importance=0.7 if category != "stale_fact" else 0.3,
                    metadata={"slice": category, "index": i},
                )
            )

    return memories


# =============================================================================
# Experiment Configuration
# =============================================================================


@dataclass
class ExperimentConfig:
    """Configuration for a single experiment run."""

    name: str
    mode: str  # "embedded" or "server"
    tokenizer: str  # "word", "multilingual", "code"
    use_bm25: bool
    use_hybrid: bool
    dense_candidate_k: int
    bm25_limit: int
    rrf_k: int = 60
    top_k: int = 8


EXPERIMENT_CONFIGS = [
    # Baseline dense-only configs
    ExperimentConfig("dense_baseline", "embedded", "word", False, False, 24, 0),
    ExperimentConfig("dense_baseline", "server", "word", False, False, 24, 0),
    ExperimentConfig("dense_large_candidates", "embedded", "word", False, False, 100, 0),
    ExperimentConfig("dense_large_candidates", "server", "word", False, False, 100, 0),
    # BM25-only configs (server only for multilingual/code)
    ExperimentConfig("bm25_only_word", "server", "word", True, False, 0, 24),
    ExperimentConfig("bm25_only_multilingual", "server", "multilingual", True, False, 0, 24),
    ExperimentConfig("bm25_only_code", "server", "code", True, False, 0, 24),
    # Hybrid dense + BM25 / RRF configs (server only)
    ExperimentConfig("hybrid_word_rrf", "server", "word", True, True, 24, 24, 60),
    ExperimentConfig("hybrid_multilingual_rrf", "server", "multilingual", True, True, 24, 24, 60),
    ExperimentConfig("hybrid_code_rrf", "server", "code", True, True, 24, 24, 60),
    # Hybrid with larger candidate budgets
    ExperimentConfig("hybrid_large_rrf", "server", "word", True, True, 100, 100, 60),
]


# =============================================================================
# Search Result Classes
# =============================================================================


@dataclass
class SearchResult:
    """Result of a single search query."""

    query: str
    slice: str
    config_name: str
    retrieved_ids: list[str]
    scores: list[float]
    latency_ms: float
    embedding_latency_ms: float
    search_latency_ms: float


@dataclass
class ExperimentMetrics:
    """Aggregated metrics for an experiment configuration on one slice."""

    config_name: str
    mode: str
    tokenizer: str
    slice: str
    recall_at_k: dict[int, float]
    mrr_at_k: dict[int, float]
    ndcg_at_k: dict[int, float]
    avg_latency_ms: float
    p95_latency_ms: float
    embedding_latency_ms: float
    search_latency_ms: float
    num_queries: int


# =============================================================================
# Experiment Runner
# =============================================================================


class BM25ExperimentRunner:
    """Run BM25 hybrid recall experiments."""

    def __init__(
        self,
        cfg: dict,
        scope: Scope,
        use_server: bool = False,
        server_url: str = "http://127.0.0.1:6333",
    ) -> None:
        """Initialize the experiment runner.

        Args:
            cfg: Configuration dictionary.
            scope: Memory scope for the experiment.
            use_server: Whether to use Qdrant server mode.
            server_url: Qdrant server URL (for server mode).
        """
        self.cfg = cfg
        self.scope = scope
        self.use_server = use_server
        self.server_url = server_url
        self.client = None
        self.store = None
        self.embedder = None
        self.memories = []
        self.memory_ids = []
        self.collection_name = cfg["qdrant"]["collection"]
        self.http_client = None

    def setup(self):
        """Initialize client, embedder, and store."""
        if self.use_server:
            self.client = QdrantClient(url=self.server_url, timeout=30, prefer_grpc=True)
            self.http_client = httpx.Client(base_url=self.server_url, timeout=30)
        else:
            import tempfile

            self.temp_dir = tempfile.mkdtemp(prefix="qdrant_bm25_exp_")
            self.client = QdrantClient(path=self.temp_dir, force_disable_check_same_thread=True)
            # For embedded mode, we can't use BM25/hybrid yet (no text index support)
            # So we'll only run dense experiments in embedded mode

        ollama_available = os.environ.get("OLLAMA_AVAILABLE")
        if ollama_available is None:
            self.embedder = build_embedder(None, self.cfg)
        elif ollama_available.lower() == "true":
            self.embedder = build_embedder(None, self.cfg, secret_reader=os.environ.get)
        else:
            print("Ollama unavailable; using deterministic fixture embeddings")
            self.embedder = FixtureEmbedder()
        self.store = QdrantStore(self.client, self.cfg, self.embedder)
        # Use reset=False since we're using fresh temp dirs/collections
        self.store.initialize(create=True, reset=False)

        # Update runner's client reference in case store.initialize replaced it
        self.client = self.store.client

        # Generate and insert test corpus
        self.memories = generate_test_corpus(self.scope)
        self.memory_ids = [m.id for m in self.memories]

        # Insert all memories via store (handles embedding)
        records = [
            (
                m.id,
                payload(
                    m.text, m.scope, m.source, m.session_id, m.category, m.importance, m.metadata
                ),
            )
            for m in self.memories
        ]

        batch_size = 64
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            self.store.upsert(batch)

        print(f"Inserted {len(self.memories)} memories")

        # Wait for indexing
        time.sleep(1)

    def create_text_index(self, tokenizer: str):
        """Create a text index for BM25 search with scoring enabled."""
        if not self.use_server:
            raise RuntimeError("Text index creation only supported in server mode")

        tokenizer_map = {
            "word": "word",
            "multilingual": "multilingual",
            "code": "code",
        }
        qdrant_tokenizer = tokenizer_map.get(tokenizer, "word")

        # Create index via REST API with scoring enabled
        response = self.http_client.put(
            f"/collections/{self.collection_name}/index",
            json={
                "field_name": "text",
                "field_schema": {
                    "type": "text",
                    "tokenizer": qdrant_tokenizer,
                    "min_token_len": 1 if tokenizer == "code" else 2,
                    "max_token_len": 50 if tokenizer == "code" else 15,
                    "lowercase": tokenizer != "code",
                    "enable_hnsw": True,
                    "scoring": {"type": "bm25"},
                },
            },
        )

        if response.status_code != 200:
            raise RuntimeError(f"Failed to create text index: {response.text}")

        # Wait for index to be ready
        for _ in range(20):
            response = self.http_client.get(f"/collections/{self.collection_name}")
            data = response.json()
            payload_schema = data.get("result", {}).get("payload_schema", {})
            if "text" in payload_schema and payload_schema["text"].get("params", {}).get("scoring"):
                print(
                    f"Created text index with tokenizer: {tokenizer}, scoring: {payload_schema['text']['params']['scoring']}"
                )
                return
            time.sleep(0.5)

        raise RuntimeError("Text index did not become ready in time")

    def _build_scope_filter(self) -> dict:
        """Build Qdrant filter for scope."""
        conditions = [{"key": "user_id", "match": {"value": self.scope.user_id}}]
        if self.scope.agent_id is None:
            conditions.append({"key": "agent_id", "is_null": True})
        else:
            conditions.append({"key": "agent_id", "match": {"value": self.scope.agent_id}})
        return {"must": conditions}

    def search_dense(self, query: str, top_k: int, candidate_k: int) -> SearchResult:
        """Run dense-only search via store."""
        start = time.monotonic()
        vector = self.embedder.embed_query(query)
        embed_time = time.monotonic()

        hits = self.client.query_points(
            self.collection_name,
            query=vector,
            using="dense",
            query_filter=scope_filter(self.scope),
            search_params=m.SearchParams(hnsw_ef=128, exact=False) if not self.use_server else None,
            limit=candidate_k,
            score_threshold=self.cfg["search"]["min_score"],
            with_payload=True,
            with_vectors=False,
        ).points

        search_time = time.monotonic()
        results = hits[:top_k]
        return SearchResult(
            query=query,
            slice="",
            config_name="",
            retrieved_ids=[str(h.id) for h in results],
            scores=[h.score for h in results],
            latency_ms=(search_time - start) * 1000,
            embedding_latency_ms=(embed_time - start) * 1000,
            search_latency_ms=(search_time - embed_time) * 1000,
        )

    def search_bm25(self, query: str, top_k: int, limit: int) -> SearchResult:
        """Run BM25-only search via REST API."""
        start = time.monotonic()
        embed_time = time.monotonic()  # No embedding for BM25

        response = self.http_client.post(
            f"/collections/{self.collection_name}/points/query",
            json={
                "query": {"text": query},
                "using": "text",
                "filter": self._build_scope_filter(),
                "limit": limit,
                "with_payload": True,
            },
        )

        if response.status_code != 200:
            raise RuntimeError(f"BM25 search failed: {response.text}")

        data = response.json()
        points = data.get("result", {}).get("points", [])

        search_time = time.monotonic()
        results = points[:top_k]
        return SearchResult(
            query=query,
            slice="",
            config_name="",
            retrieved_ids=[str(p["id"]) for p in results],
            scores=[p["score"] for p in results],
            latency_ms=(search_time - start) * 1000,
            embedding_latency_ms=0,
            search_latency_ms=(search_time - embed_time) * 1000,
        )

    def search_hybrid_rrf(
        self, query: str, top_k: int, dense_k: int, bm25_k: int, rrf_k: int = 60
    ) -> SearchResult:
        """Run hybrid dense + BM25 search with RRF fusion via REST API."""
        start = time.monotonic()
        vector = self.embedder.embed_query(query)
        embed_time = time.monotonic()

        response = self.http_client.post(
            f"/collections/{self.collection_name}/points/query",
            json={
                "prefetch": [
                    {
                        "query": vector,
                        "using": "dense",
                        "limit": dense_k,
                        "filter": self._build_scope_filter(),
                    },
                    {
                        "query": {"text": query},
                        "using": "text",
                        "limit": bm25_k,
                        "filter": self._build_scope_filter(),
                    },
                ],
                "query": {"rrf": {"k": rrf_k}},
                "limit": top_k,
                "with_payload": True,
            },
        )

        if response.status_code != 200:
            raise RuntimeError(f"Hybrid search failed: {response.text}")

        data = response.json()
        points = data.get("result", {}).get("points", [])

        search_time = time.monotonic()
        results = points[:top_k]
        return SearchResult(
            query=query,
            slice="",
            config_name="",
            retrieved_ids=[str(p["id"]) for p in results],
            scores=[p["score"] for p in results],
            latency_ms=(search_time - start) * 1000,
            embedding_latency_ms=(embed_time - start) * 1000,
            search_latency_ms=(search_time - embed_time) * 1000,
        )

    def run_experiment(
        self, config: ExperimentConfig, queries: list[TestQuery]
    ) -> list[SearchResult]:
        """Run a single experiment configuration against all queries."""
        results = []

        # Create text index if needed (server mode only)
        if self.use_server and (config.use_bm25 or config.use_hybrid):
            self.create_text_index(config.tokenizer)

        for query in queries:
            if config.use_hybrid:
                result = self.search_hybrid_rrf(
                    query.query,
                    config.top_k,
                    config.dense_candidate_k,
                    config.bm25_limit,
                    config.rrf_k,
                )
            elif config.use_bm25:
                result = self.search_bm25(query.query, config.top_k, config.bm25_limit)
            else:
                result = self.search_dense(query.query, config.top_k, config.dense_candidate_k)

            result.query = query.query
            result.slice = query.slice
            result.config_name = config.name
            results.append(result)

        return results

    def compute_metrics(
        self, results: list[SearchResult], queries: list[TestQuery]
    ) -> list[ExperimentMetrics]:
        """Compute recall, MRR, nDCG metrics per slice."""
        query_to_relevant = {q.query: set(q.relevant_ids) for q in queries}
        {q.query: q.slice for q in queries}

        grouped = defaultdict(lambda: defaultdict(list))
        for r in results:
            grouped[r.config_name][r.slice].append(r)

        metrics_list = []
        for config_name, slices in grouped.items():
            for slice_name, slice_results in slices.items():
                relevant_map = {
                    r.query: query_to_relevant.get(r.query, set()) for r in slice_results
                }

                recalls_at_k = {1: [], 3: [], 5: [], 8: [], 10: []}
                mrrs_at_k = {1: [], 3: [], 5: [], 8: [], 10: []}
                ndcgs_at_k = {1: [], 3: [], 5: [], 8: [], 10: []}
                latencies = []
                embed_latencies = []
                search_latencies = []

                for r in slice_results:
                    relevant = relevant_map[r.query]
                    if not relevant:
                        continue

                    latencies.append(r.latency_ms)
                    embed_latencies.append(r.embedding_latency_ms)
                    search_latencies.append(r.search_latency_ms)

                    for k in [1, 3, 5, 8, 10]:
                        retrieved_k = r.retrieved_ids[:k]
                        hits = len(set(retrieved_k) & relevant)
                        recalls_at_k[k].append(hits / len(relevant))

                        mrr = 0.0
                        for i, doc_id in enumerate(retrieved_k):
                            if doc_id in relevant:
                                mrr = 1.0 / (i + 1)
                                break
                        mrrs_at_k[k].append(mrr)

                        dcg = 0.0
                        for i, doc_id in enumerate(retrieved_k):
                            if doc_id in relevant:
                                dcg += 1.0 / math.log2(i + 2)
                        ideal_dcg = sum(
                            1.0 / math.log2(i + 2) for i in range(min(len(relevant), k))
                        )
                        ndcg = dcg / ideal_dcg if ideal_dcg > 0 else 0.0
                        ndcgs_at_k[k].append(ndcg)

                if latencies:
                    metrics_list.append(
                        ExperimentMetrics(
                            config_name=config_name,
                            mode="server" if self.use_server else "embedded",
                            tokenizer="word",  # Simplified
                            slice=slice_name,
                            recall_at_k={
                                k: statistics.mean(v) if v else 0.0 for k, v in recalls_at_k.items()
                            },
                            mrr_at_k={
                                k: statistics.mean(v) if v else 0.0 for k, v in mrrs_at_k.items()
                            },
                            ndcg_at_k={
                                k: statistics.mean(v) if v else 0.0 for k, v in ndcgs_at_k.items()
                            },
                            avg_latency_ms=statistics.mean(latencies),
                            p95_latency_ms=sorted(latencies)[int(0.95 * len(latencies))]
                            if latencies
                            else 0,
                            embedding_latency_ms=statistics.mean(embed_latencies)
                            if embed_latencies
                            else 0,
                            search_latency_ms=statistics.mean(search_latencies)
                            if search_latencies
                            else 0,
                            num_queries=len(slice_results),
                        )
                    )

        return metrics_list

    def cleanup(self):
        """Clean up resources."""
        if self.store:
            self.store.close()
        if self.client:
            self.client.close()
        if self.http_client:
            self.http_client.close()
        if hasattr(self, "temp_dir") and not self.use_server:
            import shutil

            shutil.rmtree(self.temp_dir, ignore_errors=True)


# =============================================================================
# Main Experiment Entry Point
# =============================================================================


def run_full_experiment():
    """Run the complete BM25 validation experiment."""
    import shutil
    import tempfile

    temp_dir = tempfile.mkdtemp(prefix="qdrant_bm25_exp_")

    cfg = merge(
        DEFAULTS,
        {
            "qdrant": {"path": temp_dir},
            "embedding": {"dimensions": 2560},
            "search": {
                "top_k": 8,
                "candidate_k": 24,
                "min_score": None,
                "hnsw_ef": 128,
                "exact": False,
            },
        },
    )

    scope = Scope(user_id="experiment_user", agent_id="bm25_test")
    queries = ALL_QUERIES

    # Assign relevant IDs to queries based on corpus
    memories = generate_test_corpus(scope)
    mem_by_slice = defaultdict(list)
    for mem in memories:
        mem_by_slice[mem.metadata["slice"]].append(mem.id)

    for q in queries:
        if q.slice in mem_by_slice:
            q.relevant_ids = mem_by_slice[q.slice][:5]

    print(f"Total queries: {len(queries)}")
    print(f"Total memories: {len(memories)}")
    print(f"Slices: {set(q.slice for q in queries)}")

    all_results = []
    all_metrics = []

    # Run experiments for embedded mode (dense only) - fresh runner per config with fresh temp dir
    print(f"\n{'=' * 60}")
    print("Running experiments in EMBEDDED mode (dense only)")
    print(f"{'=' * 60}")

    embedded_configs = [
        c
        for c in EXPERIMENT_CONFIGS
        if c.mode == "embedded" and not c.use_bm25 and not c.use_hybrid
    ]

    for config in embedded_configs:
        # Fresh temp directory for each config to avoid client closure issues
        import tempfile

        config_temp_dir = tempfile.mkdtemp(prefix="qdrant_bm25_exp_")
        config_cfg = merge(cfg, {"qdrant": {"path": config_temp_dir}})

        print(f"\nRunning config: {config.name}")
        runner = BM25ExperimentRunner(config_cfg, scope, use_server=False)
        runner.setup()

        try:
            results = runner.run_experiment(config, queries)
            all_results.extend(results)

            metrics = runner.compute_metrics(results, queries)
            all_metrics.extend(metrics)

            for metric in metrics:
                print(
                    f"  Slice: {metric.slice:15s} Recall@8={metric.recall_at_k[8]:.3f} "
                    f"MRR@5={metric.mrr_at_k[5]:.3f} nDCG@8={metric.ndcg_at_k[8]:.3f} "
                    f"Latency={metric.avg_latency_ms:.1f}ms"
                )
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback

            traceback.print_exc()
        finally:
            runner.cleanup()
            # Clean up temp directory
            shutil.rmtree(config_temp_dir, ignore_errors=True)

    # Run experiments for server mode (if available) - fresh runner per config
    print(f"\n{'=' * 60}")
    print("Checking SERVER mode availability")
    print(f"{'=' * 60}")

    server_available = False
    try:
        test_client = QdrantClient(url="http://127.0.0.1:6333", timeout=5)
        test_client.get_collections()
        test_client.close()
        server_available = True
    except Exception as e:
        print(f"Server not available: {e}")

    if server_available:
        print(f"\n{'=' * 60}")
        print("Running experiments in SERVER mode")
        print(f"{'=' * 60}")

        server_configs = [c for c in EXPERIMENT_CONFIGS if c.mode == "server"]

        for config in server_configs:
            # Use unique collection name per config to avoid conflicts
            config_collection = f"{cfg['qdrant']['collection']}_{config.name}"
            config_cfg = merge(cfg, {"qdrant": {"collection": config_collection}})

            print(f"\nRunning config: {config.name} (tokenizer={config.tokenizer})")
            runner = BM25ExperimentRunner(config_cfg, scope, use_server=True)
            runner.setup()

            try:
                results = runner.run_experiment(config, queries)
                all_results.extend(results)

                metrics = runner.compute_metrics(results, queries)
                all_metrics.extend(metrics)

                for metric in metrics:
                    print(
                        f"  Slice: {metric.slice:15s} Recall@8={metric.recall_at_k[8]:.3f} "
                        f"MRR@5={metric.mrr_at_k[5]:.3f} nDCG@8={metric.ndcg_at_k[8]:.3f} "
                        f"Latency={metric.avg_latency_ms:.1f}ms"
                    )
            except Exception as e:
                print(f"  ERROR: {e}")
                import traceback

                traceback.print_exc()
            finally:
                runner.cleanup()
                # Clean up collection
                try:
                    cleanup_client = QdrantClient(url="http://127.0.0.1:6333", timeout=5)
                    if cleanup_client.collection_exists(config_collection):
                        cleanup_client.delete_collection(config_collection)
                    cleanup_client.close()
                except Exception:
                    pass
    else:
        print("Skipping server mode experiments")

    # Save results
    output = {
        "queries": [asdict(q) for q in queries],
        "configs": [asdict(c) for c in EXPERIMENT_CONFIGS],
        "results": [asdict(r) for r in all_results],
        "metrics": [asdict(m) for m in all_metrics],
    }

    output_path = Path("experiments/results/bm25_experiment_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to {output_path}")

    # Clean up temp directory
    shutil.rmtree(temp_dir, ignore_errors=True)

    # Print summary table
    print_summary(all_metrics)

    return all_metrics


def print_summary(metrics: list[ExperimentMetrics]):
    """Print a summary comparison table."""
    print("\n" + "=" * 100)
    print("SUMMARY: Recall@8 / MRR@5 / nDCG@8 / Avg Latency (ms)")
    print("=" * 100)

    grouped = defaultdict(lambda: defaultdict(list))
    for metric in metrics:
        grouped[(metric.mode, metric.slice)][metric.config_name].append(metric)

    for (mode, slice_name), configs in grouped.items():
        print(f"\n{mode.upper()} - {slice_name.upper()}:")
        print("-" * 80)
        print(f"{'Config':<30} {'Recall@8':>8} {'MRR@5':>8} {'nDCG@8':>8} {'Latency':>10} {'n':>3}")
        print("-" * 80)

        sorted_configs = sorted(
            configs.items(), key=lambda x: x[1][0].recall_at_k[8] if x[1] else 0, reverse=True
        )

        for config_name, m_list in sorted_configs:
            m = m_list[0]
            print(
                f"{config_name:<30} {m.recall_at_k[8]:>8.3f} {m.mrr_at_k[5]:>8.3f} "
                f"{m.ndcg_at_k[8]:>8.3f} {m.avg_latency_ms:>9.1f}ms {m.num_queries:>3}"
            )


if __name__ == "__main__":
    run_full_experiment()
