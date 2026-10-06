# Hermes Qdrant 记忆插件

[English](README.md) | [简体中文](README.zh-CN.md)

独立的 Hermes 原生 `MemoryProvider`：通过 Hermes 的可信 `ctx.llm` 提取记忆，
以 Qdrant named dense vector 保存向量和 schema-v1 payload。无需 Mem0 SDK 或
独立 LLM SDK，不修改 Hermes 核心，不发送插件遥测。

## 安装与启用

需要 Python 3.11+、兼容的 Hermes 和可访问的 embedding 服务。已验证的 Hermes
提交为 `4787e4d56fc8d9265d4c7d3c0fe5accee86b4078`，尚未声明最低兼容 release tag。
默认 embedding 为 Ollama `qwen3-embedding:4b`，维度 2560；请准备足够的本机资源。

明确选择当前 profile 的 home，不要复用其他 profile 的数据：

```bash
export HERMES_HOME="/absolute/path/to/your/hermes-profile"
mkdir -p "$HERMES_HOME/plugins"
git clone \
  https://github.com/cnkang/hermes-plugin-qdrant-memory.git \
  "$HERMES_HOME/plugins/qdrant-memory"
ollama pull qwen3-embedding:4b
# 如果 Ollama 尚未运行，先启动服务，并保持服务可用。
hermes memory setup
hermes config set memory.provider qdrant-memory
```

依赖由 Hermes PM 准备，不要向 Hermes 管理的环境直接 pip install。
支持仓库安装的 Hermes 版本也可以使用
`hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory`。

本地开发也可以将 checkout 链接到一个专用、可丢弃的 profile：

```bash
mkdir -p "$HERMES_HOME/plugins"
ln -s /path/to/hermes-plugin-qdrant-memory "$HERMES_HOME/plugins/qdrant-memory"
hermes memory setup
hermes config set memory.provider qdrant-memory
```

修改配置后重启相应 Hermes runtime 或开始新会话。插件不会在进行中的会话里
重建系统提示或工具集合，以保持 prompt cache。包安装方式提供
`hermes_agent.memory_providers` entry point，并保留同目录的 CLI 和配置接口。

## 已实现能力

- embedded、自托管 Server 和 Qdrant Cloud 使用统一 client。
- Ollama `/api/embed` 与 OpenAI-compatible `/v1/embeddings`；校验响应数量、
  维度、有限非零向量和 embedding pipeline fingerprint。
- 继承主 LLM、Hermes auxiliary task，以及通过宿主信任门禁的 provider/model 覆盖。
- user/agent 作用域检索，按精确 ID 校验后更新或删除。
- turn 先写本地持久化账本，再由串行后台 worker 提取和提交；会话缓存检索。
- 基于权威 `previous_content` 镜像 builtin memory，支持重试和崩溃恢复。
- Mem0 JSON/Qdrant 只读迁移，保留来源 ID、支持 dry-run/resume/verify 和增量更新。

相似度仅用于筛选需要复核的候选。事实关系 `SAME` 才跳过，`SUPERSEDES` 更新，
`CONFLICT` 新增并记录关联，`UNRELATED` 新增。迁移按 source ID 映射，不做语义合并。

四个模型工具为 `qdrant_memory_search`、`qdrant_memory_add`、
`qdrant_memory_update`、`qdrant_memory_delete`，维护操作通过 CLI 完成：

```bash
hermes qdrant-memory status
hermes qdrant-memory doctor
hermes qdrant-memory stats
hermes qdrant-memory verify
hermes qdrant-memory retry
```

`status` 不访问网络；`doctor`/`verify` 探测已有 collection，不创建或修复它。
`retry` 重试已准备的操作，并让原始失败事件在下次 provider 启动时重新提取。
维护 embedded 数据库前停止 agent：本地持久化只允许一个 client 进程持有锁。

## 配置与迁移

行为配置位于 `$HERMES_HOME/qdrant-memory.json`，凭据放在当前 profile 的 Hermes
secret scope。默认 embedded 数据位于 `$HERMES_HOME/qdrant-memory/qdrant`。
Server 默认地址为 `http://127.0.0.1:6333`；Cloud 要求 HTTPS 和 Database API key。
详细示例、默认值、OpenAI-compatible embedding、LLM 路由及显式 fallback 见
[配置文档](docs/configuration.md)（英文）。

迁移前备份来源，并使用独立目标 collection：

```bash
hermes qdrant-memory migrate mem0 --source-json /path/to/export.json --dry-run
hermes qdrant-memory migrate mem0 --source-json /path/to/export.json \
  --target-collection hermes_qdrant_memory --resume --verify
```

默认重新 embedding。`--resume` 匹配相同 snapshot、目标和 pipeline；失败操作需要
`--retry-failed`。验证逐条检查 ID、scope 和 payload hash，数量相等不足以证明迁移
正确。来源 collection 不会被修改；返回 Mem0 时修改 provider 配置并重启。
支持的输入、Qdrant 来源和回退步骤见[迁移文档](docs/migration-from-mem0.md)（英文）。

## 运行、安全与限制

SQLite 账本保存原始 turn 和待提交记忆，应按敏感数据处理。停止 agent 后一起备份
账本与 embedded 数据库。不要通过删除 `state.db` 恢复失败；先排查配置与服务再 retry。
embedding 模型、维度或 fingerprint 改变时需要新 collection 和显式迁移。

工具参数不能覆盖调用者 scope；bot 和非 primary agent 不自动写记忆。召回文本是不
可信数据。安全、恢复流程和设计说明见[安全](docs/security.md)、
[运维](docs/operations.md)、[故障排查](docs/troubleshooting.md)、
[架构](docs/architecture.md)文档（英文）。

v0.1 实现 dense retrieval。Hybrid、RRF/DBSF、rerank、recency weighting 属于后续
范围；不支持的检索模式会被拒绝。embedding `inherit` 需要显式 `inherit_fallback`；
宿主 facade 还必须暴露 dimensions、fingerprint 和两个 embedding 方法。当前未声明
Hermes 已提供全局 embedding facade。远程 Server/Cloud 有 client 合约测试，尚无
认证端点实测；其他验证边界见[验证记录](docs/validation.md)。

## 开发验证

从 Hermes checkout 使用 PM 构建隔离环境，再运行宿主 canonical runner：

```bash
cd /path/to/hermes-agent
python -m pm.build_env --source /path/to/hermes-agent --group test \
  --export-requirements /tmp/hermes-qdrant-test-requirements.txt
python -m pm.build_env --out /path/to/hermes-plugin-qdrant-memory/.test-env \
  --requirements /tmp/hermes-qdrant-test-requirements.txt \
  --requirement 'qdrant-client>=1.15,<2' --requirement 'httpx>=0.28,<1' \
  --requirement 'pytest-cov>=6,<8'
HERMES_PYTHON=/path/to/hermes-plugin-qdrant-memory/.test-env/bin/python \
  scripts/run_tests.sh /path/to/hermes-plugin-qdrant-memory/tests -j 1 --file-retries 0
```

在插件 checkout 中运行真实 embedding pilot：

```bash
PYTHONPATH=/path/to/hermes-agent .test-env/bin/python scripts/evaluate.py
```

评估使用临时 embedded 数据库和 UTF-8 JSON；自定义数据集包含带 `id`/`text` 的
`memories`，以及带 `query`/`relevant_ids` 的 `queries`。12-topic 合成 pilot 只用于
回归基线，不代表生产召回质量。

提交前运行 Ruff lint 和格式检查，并按[开发指南](docs/development.md)（英文）
安装仓库 pre-commit hook。hook 检查暂存内容；CI 的 lint、Python 测试矩阵和
Snyk 并行运行，SonarCloud 等待覆盖率结果。

CI 包含 Python 3.11/3.14、SonarCloud 与 Snyk 依赖/源码扫描；必需 gate 会拒绝
failed/cancelled/skipped 扫描，fork 代码拿不到扫描 token。CodeRabbit 是独立
GitHub App，可能要求手动触发。见[CI 配置](docs/ci.md)（英文）。

## 许可证

MIT，Copyright © 2026 Kang Liu。详见 [LICENSE](LICENSE)。
