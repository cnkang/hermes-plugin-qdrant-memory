# Qdrant Memory installation

Copy this prompt to your Agent:

> Please read the [Qdrant Memory installation guide](https://github.com/cnkang/hermes-plugin-qdrant-memory/blob/main/INSTALL.md). First identify my Agent, active profile, and existing memory provider or data. Tell me which installation route you chose. Install only within the scope I authorize, then verify one real write and recall from a new session. Do not migrate or overwrite existing memories.

## Before installing

If the Agent has terminal access, run read-only checks first and report the
selected route before making changes. If it cannot use a terminal, ask the user
to run those checks and share only redacted output. Never ask for secret values.
Run interactive commands in the host terminal and follow their prompts; these
steps are not unattended scripts.

For Hermes, check `hermes --version`, `hermes config path`,
`hermes config get memory.provider`, `hermes memory status`, and
`hermes plugins list --plain --no-bundled`. If using Ollama, check `ollama ps`
and `ollama list`. Inspect the chosen profile's provider, Qdrant mode,
collection, embedding model and dimensions before setup.

As checked on 2026-11-26, the official Hermes Catalog installs Runtime 0.7.3
from immutable commit
[`62cf418f3de3ab59dd93989eb5f5c40131317605`](https://github.com/cnkang/hermes-plugin-qdrant-memory/commit/62cf418f3de3ab59dd93989eb5f5c40131317605).
The latest published Runtime is 0.7.3 at
[`62cf418f3de3ab59dd93989eb5f5c40131317605`](https://github.com/cnkang/hermes-plugin-qdrant-memory/commit/62cf418f3de3ab59dd93989eb5f5c40131317605).
The Catalog pin can lag PyPI and the latest Runtime source; a PyPI release does
not update the Catalog automatically.

Runtime 0.7.3 is published on PyPI, and the source pin above is the commit that
the release tag points at. Do not substitute this branch or an unreviewed commit
for the release tag.

## 1. Hermes with a new memory destination

Use the active Hermes profile. If there is no profile yet, create one through
the Hermes CLI and confirm its path before installing. Do not point a new setup
at a profile that already contains memory data.

Check whether Ollama is running and whether the selected embedding model is
already present. Reuse `qwen3-embedding:4b` if it exists. Download it only if it
is missing and the user authorizes the download. Do not run `ollama pull` as a
default step. The service owner must start Ollama if it is closed.

The default route is the official Hermes Plugin Catalog and Hermes's native
provider workflow:

```sh
hermes plugins install qdrant-memory
hermes plugins enable qdrant-memory
hermes memory setup
```

Follow the interactive setup to select Qdrant and the embedding configuration.
Then check `hermes memory status` to confirm `qdrant-memory` is the active
provider and run `hermes plugins doctor qdrant-memory` for host registration
health. Start a new Hermes session after changing the provider. Do not install
a second Qdrant MCP server into the same Hermes profile.

For Qdrant collection health, invoke the `qdrant-memory` executable from the
enabled plugin's Hermes PM environment (use its full path if it is not on
`PATH`). On macOS and Linux, locate it under the active profile's
`$HERMES_HOME/installs` directory; Windows places it under `Scripts` instead
of `bin`. Run `qdrant-memory status` first. If this is a confirmed new, empty
destination and status reports that the collection is not initialized, run
`qdrant-memory init` interactively, then run `qdrant-memory doctor`. A doctor
report that says “not initialized” before this step is expected for a new
destination. If status or doctor shows an existing collection, stop and inspect
it read-only; do not run `init`. The current Hermes CLI does not expose
`hermes qdrant-memory ...` subcommands.

If the user specifically needs the latest published Runtime instead of the
Catalog pin, use Hermes's native plugin installer with its verified immutable
commit SHA:

```sh
hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory --ref 62cf418f3de3ab59dd93989eb5f5c40131317605
```

Never substitute `main` or a development SHA. Hermes PM manages plugin
dependencies; do not pip-install into Hermes's managed environment.

To verify automatic memory, have the Agent capture a synthetic, non-sensitive
fact in a normal Hermes conversation, close that session, then ask for the fact
in a new session. Confirm the actual memory record or event result. A manual
`add` or `search` call tests explicit tools only; it does not prove automatic
extraction or prefetch.

### Verification levels

Each level proves a different thing. Do not report a higher level from a lower
one.

| Level | What it proves | Evidence to read |
|---|---|---|
| Installed | The package and its plugin entry exist | `hermes plugins list`, `hermes --version` |
| Configured | The selected profile uses this provider and destination | `hermes memory status`, `qdrant-memory status` |
| Connected | The interface reaches storage with the configured embedding pipeline | `qdrant-memory doctor` (payload indexes, embedding probe, fingerprint) |
| Used | A real record survives a session boundary and changes a later result | The cross-session example below |

### Cross-session example

Use synthetic content only. Each step names what to observe.

1. In one session, save one durable preference through an authorized explicit
   tool call:

   ```json
   {"text": "Project Canary prefers release notes in English."}
   ```

   Expected result: an `ADD` action and an exact ID. A `SKIP` means an identical
   record already exists in this scope.

2. End the session. Start a new session with the same profile and the same
   memory interface.

3. Ask for work that depends on that preference: "Draft the release note for
   Project Canary." The agent should search before it writes.

4. Read the search result. A useful result contains the stored record, its ID
   and a score. The release note should follow the stored preference.

5. Confirm both parts. A tool call without a stored record, or a record that
   did not change the output, is not evidence of cross-session memory.

Automatic extraction follows the same flow without an explicit call. State the
preference in a normal conversation, end the session, then verify the record or
the event result in the new session.

## 2. Hermes with existing memory data or provider

Do not use the new-destination route. First perform only read-only checks of the
active profile, selected provider, Qdrant mode, collection, embedding model and
dimensions, and relevant secret *names*. For Cloud and self-hosted Qdrant,
inspect collection metadata without writing to it. Keep secret values in the
host's environment-variable support or secret store; never echo, log or copy
them into a document.

Treat installation, provider switching and migration as separate actions. Do
not initialize an existing collection, clear or recreate it, change its
embedding model or dimensions, migrate data, stop a service, or change
production configuration without explicit authorization for that action. If
the user authorized installation only, leave the current provider and data in
place and report what additional authorization is needed.

## 3. Codex and other non-Hermes Agents

Use the public, version-pinned stdio MCP route in the
[MCP Quickstart](https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/quickstart.md)
(`qdrant-memory-mcp==0.3.5` as checked on 2026-11-17). Give it a dedicated
profile. Run its `init` and `doctor` steps before registering the stdio server,
then have the client discover the four `qdrant_memory_*` tools. Follow the
Quickstart's host configuration and verification steps; it records tool results
and checks recall again after reconnecting.

Standard MCP exposes explicit memory operations. It does not automatically
extract memories from conversations or prefetch them into prompts. Do not add
this MCP server to a Hermes profile that already uses the native Qdrant provider.

If an Agent has no terminal access, ask the user to perform each terminal step
and share only redacted status and tool-discovery output. The Agent must not
claim installation or write/recall verification it did not observe.

## Advanced integrations

- [DSH integration status and setup](https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/dsh-integration.md)
- [WorkBuddy Connector](https://github.com/cnkang/qdrant-memory-mcp/blob/main/integrations/workbuddy/README.md)
- [Codex-specific plugin candidate and verification](https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/codex-plugin-verification.md)
- [Shared Owner deployment](https://github.com/cnkang/hermes-plugin-qdrant-memory/blob/main/docs/shared-memory/README.md)

## 中文

把这段提示发给 Agent：

> 请先读取[Qdrant Memory 安装指南](https://github.com/cnkang/hermes-plugin-qdrant-memory/blob/main/INSTALL.md)，识别我使用的 Agent、当前 profile 和已有记忆 provider 或数据。告诉我你选择的安装方式。只在我授权的范围内安装，并用一次真实写入和新会话召回验证结果。不要迁移或覆盖已有记忆。

### 安装前

如果 Agent 有本机终端权限，先只读检查宿主与当前 profile，并先告诉用户选择的路径。
如果没有终端权限，请用户运行只读检查并分享已脱敏的输出。不要索要密钥值。互动式
命令应由用户在宿主终端逐步确认；这些步骤不是无人值守脚本。

Hermes 检查 `hermes --version`、`hermes config path`、
`hermes config get memory.provider`、`hermes memory status` 和
`hermes plugins list --plain --no-bundled`。使用 Ollama 时检查 `ollama ps` 与
`ollama list`。在 setup 前确认选中 profile 的 provider、Qdrant 模式、collection、
embedding 模型与维度。

截至 2026-11-26，官方 Hermes Catalog 安装 Runtime 0.7.3，固定提交为
[`62cf418f3de3ab59dd93989eb5f5c40131317605`](https://github.com/cnkang/hermes-plugin-qdrant-memory/commit/62cf418f3de3ab59dd93989eb5f5c40131317605)。
最新已发布 Runtime 是 0.7.3，提交为
[`62cf418f3de3ab59dd93989eb5f5c40131317605`](https://github.com/cnkang/hermes-plugin-qdrant-memory/commit/62cf418f3de3ab59dd93989eb5f5c40131317605)。
Catalog pin 可能落后于 PyPI 和最新 Runtime 源码；发布 PyPI 不会自动更新 Catalog。

Runtime 0.7.3 已发布到 PyPI，上面的源码 pin 就是发行 tag 指向的提交。不要用本分支或
未经审核的 commit 替代发行 tag。

### 1. Hermes 新建记忆目的地

使用当前活动 Hermes profile。如果还没有 profile，通过 Hermes CLI 创建并确认路径后再
安装。不要把新安装指向已有记忆数据的 profile。

先确认 Ollama 正在运行，并检查所选 embedding 模型是否已经存在。若已有
`qwen3-embedding:4b` 就复用；只有模型缺失且用户授权下载时才下载。不要把
`ollama pull` 作为默认步骤。若 Ollama 已关闭，应由服务所有者先启动。

默认使用官方 Hermes Plugin Catalog 和 Hermes 原生 provider 流程：

```sh
hermes plugins install qdrant-memory
hermes plugins enable qdrant-memory
hermes memory setup
```

按互动式设置选择 Qdrant 与 embedding 配置。随后用 `hermes memory status` 确认
`qdrant-memory` 已选为活动 provider，并运行 `hermes plugins doctor qdrant-memory`
检查宿主注册。切换 provider 后启动新的 Hermes 会话。同一个 profile 不要再装一套
Qdrant MCP。

检查 Qdrant collection 时，调用已启用插件 Hermes PM 环境中的 `qdrant-memory` 执行文件；
若它不在 `PATH`，使用完整路径。macOS/Linux 的执行文件位于活动 profile 的
`$HERMES_HOME/installs` 子目录；Windows 使用 `Scripts` 而非 `bin`。先运行
`qdrant-memory status`。只有确认是新的空目的地，且 status 显示 collection 尚未初始化，
才在交互终端运行 `qdrant-memory init`，然后运行 `qdrant-memory doctor`。新目的地在 init
前由 doctor 报告“尚未初始化”是预期状态。如果 status 或 doctor 发现已有 collection，
请停止并只读检查，不要运行 `init`。当前 Hermes CLI 没有 `hermes qdrant-memory ...` 子命令。

如果用户明确需要最新已发布 Runtime 而不是 Catalog pin，使用 Hermes 原生插件安装器并
指定已验证的不可变提交 SHA：

```sh
hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory --ref 62cf418f3de3ab59dd93989eb5f5c40131317605
```

不要替换成 `main` 或开发 SHA。插件依赖由 Hermes PM 管理；不要 pip install 到 Hermes
管理的环境中。

验证自动记忆时，让 Agent 在正常 Hermes 对话中记录一个合成、非敏感事实，结束会话后在
新会话询问该事实。检查真实的记忆记录或事件结果。手动 `add` 或 `search` 只验证显式工具，
不能证明自动提取或预取。

#### 验证层级

每一层证明的内容不同。不要用较低层的证据声称达到较高层。

| 层级 | 证明什么 | 需要读取的证据 |
|---|---|---|
| 已安装 | 包与插件入口存在 | `hermes plugins list`、`hermes --version` |
| 已配置 | 选中 profile 使用该 provider 与目的地 | `hermes memory status`、`qdrant-memory status` |
| 已连接 | 接口能以配置的 embedding 管线访问存储 | `qdrant-memory doctor`（payload 索引、embedding 探针、fingerprint） |
| 已使用 | 真实记录跨会话边界保留，并改变后续结果 | 下方的跨会话示例 |

#### 跨会话示例

只使用合成内容。每一步都说明需要观察的结果。

1. 在一个会话中，用获得授权的显式工具调用保存一条长期偏好：

   ```json
   {"text": "Project Canary 的发布说明使用英文。"}
   ```

   预期结果：动作为 `ADD`，并返回精确 ID。返回 `SKIP` 说明该作用域内已存在相同记录。

2. 结束该会话。使用同一 profile 和同一记忆接口启动新会话。

3. 提出依赖该偏好的任务：“为 Project Canary 起草发布说明。” Agent 应先检索再动笔。

4. 阅读检索结果。有效结果包含已保存的记录、其 ID 与分数。发布说明应符合该偏好。

5. 同时确认两部分。只有工具调用而没有对应记录，或有记录但输出未改变，都不算跨会话记忆
   生效的证据。

自动提取走同一流程，只是不需要显式调用：在普通对话中说明该偏好，结束会话，再在新会话中
核实记录或事件结果。

### 2. Hermes 已有记忆数据或 provider

不要走新建目的地路径。先只读检查当前 profile、选中 provider、Qdrant 模式、collection、
embedding 模型与维度，以及相关密钥的**名称**。对于 Cloud 与自托管 Qdrant，只读查看
collection 元数据。密钥值继续保存在宿主支持的环境变量或秘密存储中；不要回显、写日志或
复制进文档。

安装、切换 provider、迁移是三种独立操作。未经对应操作的明确授权，不要初始化现有
collection、清空或重建、改变 embedding 模型或维度、迁移数据、停服务或修改生产配置。
若用户只授权安装，就保留现有 provider 与数据，并说明还需要哪项授权。

### 3. Codex 与其他非 Hermes Agent

优先使用公开固定版本的 stdio MCP 路径：[MCP Quickstart](https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/quickstart.zh-CN.md)。
截至 2026-11-17，版本为 `qdrant-memory-mcp==0.3.5`。使用独立 profile，按 Quickstart
先运行 `init` 和 `doctor`，再注册 stdio 服务，让客户端发现四个 `qdrant_memory_*` 工具。
按照 Quickstart 的宿主配置和验收步骤记录工具返回结果，并在断开重连后再次检查召回。

普通 MCP 只提供显式记忆操作，不会自动从对话提取记忆，也不会自动预取到提示词。同一个
Hermes profile 已使用原生 Qdrant provider 时，不要再注册这套 MCP。

如果 Agent 没有终端权限，请用户逐步运行命令并分享已脱敏的状态与工具发现结果。Agent 不得
声称完成了自己没有观察到的安装、写入或召回验收。

### 进阶集成

- [DSH 接入状态与设置](https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/dsh-integration.zh-CN.md)
- [WorkBuddy Connector](https://github.com/cnkang/qdrant-memory-mcp/blob/main/integrations/workbuddy/README.md)
- [Codex 专用插件候选与验收](https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/codex-plugin-verification.md)
- [Shared Owner 部署](https://github.com/cnkang/hermes-plugin-qdrant-memory/blob/main/docs/shared-memory/README.zh-CN.md)
