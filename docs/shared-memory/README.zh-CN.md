# One Memory, Multiple Agents — 可选启用的共享 owner

生产部署前，请阅读[受保护目的地与诊断指南](deployment-security.md)和[威胁模型](threat-model-v1.md)。
Runtime 0.5.0 包含可选启用的目的地保护。在应用客户端中，
Qdrant 写凭据必须只属于 Owner。

多个独立 Agent 可以通过同一个 Runtime owner 使用一份规范存储。本 checkout 实现了
本地认证服务和 stdio MCP 桥接。现有 Hermes Native Provider、独立 stdio MCP、DSH 和
WorkBuddy 的默认行为保持私有且不变。本次工作不会重新配置任何已安装的 profile。

见[架构/安全契约](architecture.md)、[英文操作指南](README.md)和
[实测验证报告](report.md)。不要在生产 profile 上试验下方命令。

## 安装并创建独立的 owner profile

使用 Python 3.11+ 和隔离环境。Runtime 0.5.0 包含此功能。请安装 PyPI 发行版，或在开发
时使用固定 commit 的已审核 checkout。

安装包前，请检查 [PyPI](https://pypi.org/project/hermes-plugin-qdrant-memory/0.5.2/)
是否已提供 Runtime 0.5.2。

```sh
python3 -m venv /absolute/path/shared-runtime-env
/absolute/path/shared-runtime-env/bin/pip install 'hermes-plugin-qdrant-memory[mcp]==0.5.2'
/absolute/path/shared-runtime-env/bin/qdrant-memory setup \
  --profile /absolute/path/shared-owner --embedding-provider ollama \
  --model qwen3-embedding:0.6b --dimensions 1024 \
  --base-url http://127.0.0.1:11434 --user-id owner-admin
```

运行 `setup` 前请先显式安装所选的 Ollama 模型。`setup` 会创建一个新的私有 embedded profile
并校验其 collection。也可以使用现有 runtime 配置和 `init` 流程，在 owner profile 中
配置远程 Qdrant Server 或 Cloud。这些凭据只能保存在 owner 中。绝不要让多个独立
profile 指向同一目的地进行写入。所有客户端 embedding 都在 owner 上运行。fingerprint、
距离和维度必须与现有 collection 一致。更换模型需要显式创建新目的地，并执行现有的
授权迁移流程。

在新的运维方自有目录中，将三个独立的高熵 token 生成到私有文件和一个 policy 中，
且不要打印凭据：

```python
import hashlib, json, os, secrets
from pathlib import Path

os.umask(0o077)
clients = []
for principal, permission in [("hermes", "write"), ("dsh", "write"), ("reviewer", "read")]:
    token = secrets.token_urlsafe(32)
    path = Path(principal + ".token")
    with path.open("x") as stream:
        stream.write(token)
    clients.append(
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal,
            "owner_id": "owner-admin",
            "space_id": "my-project",
            "permission": permission,
            "visibility": "shared",
        }
    )
with Path("policy.json").open("x") as stream:
    json.dump({"clients": clients}, stream, indent=2)
```

这些文件必须排除在 Git 之外。policy 必须为 POSIX 0600 模式（Windows 上使用私有 ACL）。
`visibility: project` 是另一种显式授予的空间。`visibility: private` 在每个 owner/空间
中只能有一个 principal。一份凭据只授予一个已配置空间。访问其他空间请使用不同的凭据和
进程。

从包含策略文件的私有目录启动 Owner。解析后的 `--policy` 路径必须位于当前工作目录内。

```sh
cd /absolute/path
/absolute/path/shared-runtime-env/bin/qdrant-memory \
  --home /absolute/path/shared-owner shared-serve \
  --policy /absolute/path/policy.json --port 8765
```

等待 readiness JSON。端点只监听 loopback。它是私有 API，**不是** HTTP MCP 端点。
不要把它放在公共反向代理之后。审计的允许/拒绝决定写入 stderr，principal ID 以哈希
形式出现。请按本地隐私策略采集并保留这些日志。按 Ctrl-C 停止。服务关闭时会先排空
活动请求，再释放 WriterLease。

## 私有、共享、只读和项目空间

凭据在可信配置中绑定 `principal`、`owner_id`、`space_id`、`permission` 和
`visibility`。`permission: read` 在服务边界拒绝所有写入。`write` 允许读取和经授权的
修改。`visibility: private` 空间只允许一个 principal。`shared` 或 `project` 空间可
显式授予多个 Agent。一个 token 只连接一个空间，访问其他空间需使用不同凭据和连接。

owner 映射到现有 `user_id`。稳定空间映射到保留的 `agent_id: shared:v1:<space>`。
创建者存入 `created_by`，最后更新者存入 `updated_by`。搜索同时校验 scope、共享标记
和 visibility。ID 修改校验同一边界。没有显式共享标记的记录保持私有，原有的
`agent_id: null` 不是跨 Agent 通配符。工具参数不能覆盖 owner、Agent 身份或空间。
共享永远不会导入私有对话历史。

## 接入 DSH、WorkBuddy、Codex 或 Hermes 的 MCP fallback

各宿主从预览环境启动自己的桥接：

```json
{
  "mcpServers": {
    "qdrant-memory-shared": {
      "command": "/absolute/path/shared-runtime-env/bin/qdrant-memory",
      "args": ["shared-mcp", "--url", "http://127.0.0.1:8765"],
      "env": {"QDRANT_MEMORY_SHARED_TOKEN": "<inject this host's own token securely>"}
    }
  }
}
```

该占位符不是可用凭据。请使用宿主支持的密钥注入机制。绝不要把真实 token 粘入受源码
控制的示例。stdio 工具名仍为 `qdrant_memory_search/add/update/delete`。共享模式为
修改操作增加必需的 `request_id`。update 和 delete 还需要 `expected_version`。
请以实际工具目录为准。
Codex 的 TOML MCP 配置使用：

```toml
[mcp_servers.qdrant-memory-shared]
command = "/absolute/path/shared-runtime-env/bin/qdrant-memory"
args = ["shared-mcp", "--url", "http://127.0.0.1:8765"]
env_vars = ["QDRANT_MEMORY_SHARED_TOKEN"]
```

这是显式的外部 MCP 注册，不是对受管 bundle 的修改。DSH bundle 和 WorkBuddy 托管安装器
仍启动各自的私有 profile。仓库附带的 MCP 分发包含单独的预览配置示例。宿主 UI 或原生
bundle 导入的验收仍是后续工作。

**Hermes 兼容性：** Native Provider 默认保持私有，并执行现有的自动提取/预取。
Runtime 0.5.0 可显式启用 Native Owner 共享提取，配置见[远程 Owner 指南](remote-owner.md)。仅对未启用 qdrant-memory Native Provider 接口的 Hermes
profile 使用共享 MCP 桥接。不要为同一个 Hermes profile 注册两种接口。另一种做法是
保留 Native Provider，并用显式运维 CLI 执行用户授权的共享保存。不伪造任何生命周期
回调。

## 操作示例：Hermes 显式保存，独立 DSH 召回

该流程在 runtime/stdio 接口层面可用。实际宿主应用 UI 流程未获 SDK 测试认证。
对于使用 MCP fallback 的 Hermes profile：

1. 用户授权将一项项目决定保存到 `my-project`。Hermes 使用自己的凭据和
   `qdrant_memory_add`，参数为
   `{"text":"Project uses SQLite WAL","request_id":"decision-save-001"}`。
2. 新的 DSH 会话使用自己的凭据，以
   `{"query":"Which database mode does this project use?"}` 搜索。服务返回规范记录，
   其中包含 `created_by: hermes`、ID 和不透明 `version`。
3. 经授权替换时，DSH 提供该精确 ID/version：
   `{"id":"<id>","text":"Project uses PostgreSQL","expected_version":"<version>","request_id":"decision-edit-001"}`。
4. Hermes 再次搜索即可看到新文本/版本。基于旧版本的修改返回 `SharedConflictError`，
   绝不会覆盖较新的修改。
5. reviewer 凭据可以搜索，但所有 add/update/delete 都被拒绝。私有记录、其他用户的
   记录或旧版记录保持不可见。

直接 CLI 调用使用同一策略边界：

```sh
# 先通过可信的本地密钥机制导出该 client 的 token。
qdrant-memory shared-call --url http://127.0.0.1:8765 \
  --tool qdrant_memory_search --arguments '{"query":"database mode"}'
```

不要自动共享整段对话。召回文本是不可信数据，永远不能授权保存、修改或变更授予。

## 重试、备份、恢复、升级与回滚

为每次预期修改保留稳定且唯一的 request ID。网络失败具有不确定性：请用**相同**的
request ID 和完全相同的参数重试。持久化 receipt 会返回原始结果。用同一 request ID
搭配其他参数会冲突。成功的 receipt 只代表历史状态。重连后请搜索当前状态。遇到旧版本
冲突时，重新读取并为新的修改取得授权意图。仅为该新授权操作使用新的 request ID。
绝不要猜测新版本。

owner 会在就绪前重放待处理的 Runtime 操作和未完成的请求计划。遇到终止性失败时，它
阻止新的写入并要求人工介入。停止 owner，恢复 Qdrant/embedding 依赖，运行现有运维命令
`qdrant-memory --home OWNER retry`，然后重启。已登记的 destination 还必须提供受保护
policy：先切换到 policy 所在目录，`--home` 使用绝对路径，再追加
`--policy ./owner-policy.json`。policy 路径必须位于当前调用目录之内。缺少这一显式
输入时，命令会拒绝已登记的 destination，而不是打开它。不得同时运行第二个 owner 执行维护。
共享计划未完成期间，共享写入和会修改数据的 init/delete/import 命令会 fail-closed；共享搜索仍可用。
运维 retry 仍可用，之后重启 owner 以核对 receipt。待处理的破坏性意图和原始宿主提取
事件继续阻止不兼容的启动。请使用独立的 owner home，不要复用 Native Provider 的活动
profile。

停止 owner，并对**整个 profile（包括 SQLite WAL/sidecar、Qdrant embedded 数据和配置）**
做一致性备份。对于远程 Qdrant，应在 owner 停止时协调服务端 snapshot 与其 profile 备份。
请单独保存 policy 和客户端 token，并设置私有权限。恢复时须同时匹配账本/存储快照。
不要只恢复已完成操作的一侧。绝不要把打开的 embedded 目录复制当作一致性保证。可移植的
Mem0 格式 export 仍是交换操作，不是完整的共享备份：它不保留共享授予、修订 token 或
共享 request receipt。重新导入的记录在显式执行受支持的共享操作前保持私有。

该功能新增一张 request 表和可选 payload 字段。现有 collection 只做校验，绝不静默迁移。
旧代码会忽略这些新增表和字段。回滚时，停止服务，保留一致性备份，移除新的 MCP 注册，
并将 Agent 恢复到原先的私有 profile。不要用旧版独立工具修改由服务管理的共享 scope。
旧版 owner 无法核对未完成的共享请求计划。回滚前请保留新 Runtime 可用，以排空并恢复
这些计划。降级**不是**待处理共享计划的安全恢复方式。

## 限制与排障

- `WriterBusyError`：使用规范服务，或干净地停止旧 owner。绝不要删除锁。分离的
  profile home 不会增加安全的多写入支持。
- `PermissionError`：检查宿主 token/授予和 ID 空间。不要伪造身份。
- `SharedConflictError`：旧修订或复用的 request ID。请显式核对。
- `SharedRecoveryRequiredError`：停止接纳，修复依赖，retry/重启。
- embedding fingerprint/维度不匹配：保留原有 pipeline，或执行带备份的显式迁移到新
  collection。绝不要更改 fingerprint。
- HTTP 403：需要精确的 loopback Host、JSON content type、无 Origin 和有效 bearer token。
- 共享不意味着质量提升。所有搜索和写入都经 owner 串行化。本地推理可能成为吞吐瓶颈。
  包括本地跳转开销在内的可复现测量见验证报告。
- MVP 为保障重试正确性会无限期保留 receipt。计划 payload 在提交后清除。尚未发布
  receipt 压缩策略。
- 未实现分布式多写、embedded 直接读取、OAuth、组管理 UI，以及私有到共享的自动迁移。
  HTTPS MCP 与 Native Owner 提取请使用独立的[远程 Owner 指南](remote-owner.md)。

官方 [Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant) 提供带 Qdrant 持久化和
metadata 过滤的语义存储/检索。Qdrant Memory 侧重 Agent 记忆生命周期、持久恢复和显式
受控共享。Runtime 0.5.0 提供此功能，仍需显式启用并由运维方配置。

## 本机 HTTP 安全边界

共享 owner 使用未加密的私有本机 HTTP API，仅绑定 `127.0.0.1`。Bearer 凭据约束已配置
的客户端。以同一 OS 用户运行的恶意代码或本机特权进程不在凭据保密边界内。
此私有 API 不得通过公共代理暴露。远程客户端必须使用 Runtime 0.5.0 的独立 HTTPS MCP 端点。

仓库记录了一项 Sonar 规则例外：`python:S5332`（明文 HTTP）仅对
`qdrant_memory/shared_service.py` 应用上述本机传输设计决定。其他所有安全规则和质量
阈值继续生效。理由和测试见[架构说明](architecture.md#reviewed-local-http-transport-decision-sonar-s5332)。
在增加远程或通配监听前，请移除此例外，并重新审核 TLS 与远程身份授权。
