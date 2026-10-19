# One Memory, Multiple Agents：跨 Agent 共享记忆（可选启用）

生产部署前请阅读[目的地保护、诊断与部署指南](deployment-security.md)和[威胁模型](threat-model-v1.md)。保护策略与 `shared-doctor` 尚未发布，不能直接用于 Runtime 0.4.0。Agent 只应持有 Owner 客户端令牌，不应持有 Qdrant 写凭据。本地锁无法约束远程管理员或同等系统权限。

多个独立 Agent 可以通过同一个持有 Runtime、Ledger 和 WriterLease 的本地服务访问同一份记忆。
它是显式选择启用的新模式；Hermes Native Provider、自动提取、现有 stdio MCP、DSH 和 WorkBuddy
私有配置均不自动切换。Hermes 兼容性是首要验收门槛。

完整命令与可复制配置见[英文操作指南](README.md)，设计与安全边界见
[架构决策](architecture.md)，实测与未完成项见[验证报告](report.md)。

## 安装和启动

在独立 Python 3.11+ 虚拟环境中安装 PyPI 上的 Runtime 0.4.0 及 `[mcp]` extra：

```sh
python3 -m venv /absolute/path/shared-runtime-env
/absolute/path/shared-runtime-env/bin/pip install 'hermes-plugin-qdrant-memory[mcp]==0.4.0'
```

使用新的 owner profile，通过原有 `setup` 显式选择 embedding 模型
并初始化集合；不要拿生产 profile 做测试。Ollama 模型须先安装，例如
`qwen3-embedding:0.6b` 对应 1024 维。集合维度、距离和 embedding fingerprint 验证继续生效。
更换模型应显式创建新目标并执行已有备份和迁移流程，不能直接改原集合。

按英文指南的 Python 示例生成高熵 token 和 `policy.json`，文件仅所有者可读写，不能进入 Git。
每个 Agent 使用不同 token。policy 保存 SHA256，不保存 token 明文。Windows 由操作员设置私有 ACL。
然后从 owner 目录启动（policy 路径必须解析在当前工作目录内）：

```sh
cd /absolute/path/shared-owner
/absolute/path/shared-runtime-env/bin/qdrant-memory \
  --home /absolute/path/shared-owner shared-serve \
  --policy /absolute/path/policy.json --port 8765
```

等待 readiness JSON。服务仅监听 `127.0.0.1`，是私有 JSON API，不是 HTTP MCP 服务。
不要代理到公网。审计 stderr 只包含哈希化身份和允许/拒绝结果，不包含记忆正文或 token。
Ctrl-C 后排空已接收请求，再释放 WriterLease。policy 修改和 token 撤销需要重启。

## 私有、共享、只读和项目空间

凭据由可信配置绑定 `principal`、`owner_id`、`space_id`、`permission` 和 `visibility`。
`permission: read` 在服务边界拒绝所有写入；`write` 允许读取和经授权的修改。
`visibility: private` 的空间只允许一个 principal；`shared` 或 `project` 可明确授予多个 Agent。
一个 token 只连接一个空间，需要访问另一个空间时使用另一份凭据和连接。

owner 映射为原 `user_id`；稳定空间映射为保留的 `agent_id: shared:v1:<space>`；创建者另存
`created_by`，更新者另存 `updated_by`。搜索同时验证 scope、共享标记和 visibility，ID 修改同样校验。
没有显式共享标记的历史记录全部保持私有，原 `agent_id: null` 也不是跨 Agent 通配符。
工具参数不能覆盖 owner、Agent 身份或空间。共享不会导入私人对话历史。

## 接入 Hermes、DSH、WorkBuddy、Codex

各 Agent 在自己的 MCP 配置中启动：

```text
/absolute/path/shared-runtime-env/bin/qdrant-memory shared-mcp --url http://127.0.0.1:8765
```

通过宿主支持的安全环境变量机制传入该 Agent 的 `QDRANT_MEMORY_SHARED_TOKEN`。
桥接进程不打开数据库、不创建 Ledger、不运行 embedding。英文指南包含 JSON 与 Codex TOML 示例。
DSH Bundle 和 WorkBuddy Managed Connector 默认仍安装和运行私有 profile；本次更新不修改已有安装。
真实宿主 UI 导入仍需后续人工验收，自动测试覆盖官方 MCP SDK 的独立进程协议连接。

**Hermes 优先兼容：Native Provider 继续处理原有私有记忆，不改变生命周期或默认 schema。**
Native Provider 自动将提取结果保存到共享空间尚未实现。只有未启用 qdrant-memory Native Provider
接口的 Hermes profile 才使用本次 MCP fallback；不要给同一 Hermes profile 注册重复接口。
保留 Native Provider 时，也可以由操作员使用 CLI 显式保存用户授权的共享事实。

## 可操作示例：Hermes 保存，DSH 新会话读取

这是已实现的 Runtime/stdio 接口流程，尚未宣称宿主应用 UI 完整认证：

1. 用户授权保存项目决定。Hermes MCP fallback 用自身 token 调用 add：
   `{"text":"项目使用 SQLite WAL","request_id":"decision-save-001"}`。
2. DSH 在独立新会话用自己的 token 搜索 `{"query":"项目使用什么数据库模式？"}`，读取同一条
   canonical 记录及 ID、`version`、`created_by: hermes`。
3. 经用户授权后 DSH 更新，传入原 ID、读取时的 `expected_version`、新正文和新的 `request_id`。
4. Hermes 下次搜索可见新内容。持旧版本修改将收到 `SharedConflictError`，不会覆盖新内容。
5. 只读 Agent 可以搜索，add/update/delete 全部拒绝；私有空间和其他用户数据无法读取。

`version` 是不透明 UUID 修订标记，不能自行推算。删除后重建也使用新标记，避免旧版本误改。
所有写入由一个 Runtime 锁串行协调，不支持分布式多写。

## 重试、恢复和维护

每个逻辑修改保留固定 `request_id` 和原参数。断线后重试同一请求，服务返回已持久化的原操作结果；
同一 ID 搭配不同参数会冲突。历史 receipt 不保证数据当前仍存在，重连后应搜索核对。
过期版本冲突需要重新读取并确认新的修改意图，不能自动拿新版本覆盖。

写入先将完整计划和预期响应保存到同一个 SQLite 的新增表，再调用原 Runtime 的 operation/commit。
服务会在启动就绪前恢复尚未完成的计划，重放同一个点 ID 和版本，不产生逻辑副本。
写入结果不确定时阻止后续写入；终止错误须先停止 owner，恢复依赖，再使用原 CLI 的
`qdrant-memory --home OWNER retry` 并重启。不要并行启动维护程序或移除锁文件。
已有 destructive recovery 和原始 Hermes extraction event 的启动阻断保持有效。

备份前停止 owner，整体保存 profile、SQLite 数据库及 WAL/sidecar、embedded Qdrant 数据和配置。
远程 Qdrant 应在 owner 停止时协调 snapshot，与 Ledger 配套恢复；凭据单独安全备份。
只恢复其中一侧不能保证一致。Mem0 格式 export 是交换格式，不保留共享授权、版本标记和 request receipt，
不能替代完整备份；重新 import 的记录继续私有。升级不自动迁移集合或旧数据。

回滚先停止服务并保留完整备份，移除新 MCP 注册，将 Agent 恢复到原私有 profile。
老版本不能恢复未完成的 shared plan，因此须保留新 Runtime 完成排空/恢复后再回滚。
不要用旧版私有工具修改 service 管理的共享 scope。

## 限制与排障

- `WriterBusyError`：连接唯一 owner 或正常关闭旧 owner；绝不能绕过锁。
- `PermissionError`：检查 token、grant 和空间；不要伪造身份扩大范围。
- `SharedConflictError`：旧版本或 request ID 重用，明确协调后再修改。
- `SharedRecoveryRequiredError`：停止写入、修复依赖、执行授权 retry 后重启。
- fingerprint/维度不匹配：恢复原 embedding，或执行显式备份迁移。
- 同一 OS 用户的恶意代码可读进程环境和 profile，不属于 bearer token 的隔离边界。
- 不同 profile/主机的直接 Qdrant 写入不受本地 WriterLease 约束，属于不支持的部署。
- 所有操作经过 owner 串行化，embedding 可能成为瓶颈；不宣称性能或检索质量提升。
- receipt 为可靠重试而保留，完成后的正文计划会清除；当前版本没有 receipt 自动压缩策略。
- 不支持公网 API、OAuth、远程 Agent 直连、分布式多写、独立进程打开 embedded 数据目录，
  不支持自动将私有历史转为共享数据。

官方 [Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant) 提供通用语义存储和检索，
具备 Qdrant 持久化与 metadata 过滤能力。本项目侧重 Agent 长期记忆生命周期、恢复和显式授权共享，
不以未经验证的方式贬低官方实现。本功能随 Runtime 0.4.0 发布，默认不启用，需要运维方配置。


### 本机 HTTP 的安全审查边界

共享 owner 使用未加密的本机私有 HTTP API，固定监听 `127.0.0.1`。
Bearer 凭据用于约束已配置客户端；同一 OS 用户的恶意代码或本机高权限进程不在
该凭据的保密边界内。当前版本不支持远程访问，不应通过公共代理暴露服务。

仓库中明确记录一项 Sonar 规则例外：仅对 `qdrant_memory/shared_service.py`
中的 `python:S5332`（明文 HTTP）应用上述本机传输设计判断。其他安全规则和质量
阈值继续执行。依据及测试见[架构说明](architecture.md#reviewed-local-http-transport-decision-sonar-s5332)。
加入远程或通配监听前必须移除此例外，重新审核 TLS 和远程身份授权方案。
