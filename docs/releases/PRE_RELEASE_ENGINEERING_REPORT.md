# v0.1.0 预发布工程加固报告

> 本文件保留了早期加固阶段的记录；PR #10 已合并。最新验证快照见
> [验证记录](../validation.md)，最终审查记录见 [PRE_RELEASE_FINAL_REVIEW.md](PRE_RELEASE_FINAL_REVIEW.md)。

## 历史验收（2026-10-08）

PR #10、#11、#12 均已合并。合并后的 main 为 `32bd4e7abdb3b417cc5b0dd0f793f93db188db3b`。
该 SHA 的 [Cloud 实测](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338838)、
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338914) 和
[六项跨平台检查](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802339623) 已通过。
Cloud 测试步骤实际执行，早期候选分支受 main-only 环境保护阻止的记录仅属于历史。
项目仍为有限技术预览，要求所有 profile/主机对每个目的地仅保留一个 writer；
尚未打标签或发布。实时交互 Quick Start 已于 2026-10-09 在全新 home 完成
（安装、自动提取、召回、更新、删除与重启，记录见[验证记录](../validation.md)）；
持久化接收点、账本保留与合成评估边界仍适用，详见[最终验收报告](PRE_RELEASE_FINAL_REVIEW.md)。

## 历史 PR #11 预发布加固（合并前记录，2026-10-08）

以下 Cloud 阻塞与 NOT READY 结论只描述 PR #11 合并前的实现提交；已被上方 main 验证更新。

- 本轮基线：远端 `main` 的 `a6a883cd3db3bfdf57cb923f1e9c26fb336df211`；工作分支为
  `codex/v0.1.0-pre-release-hardening`。用户数据未用于迁移或测试。
- 新确认的删除顺序问题：旧 UPSERT 已写入账本并失败后，若相同事实通过另一来源写入
  不同 point ID，再删除该事实，重试仍可能仅按 point ID 判断而恢复旧值。
- 修复在每次提交 UPSERT 前重新检查 scoped Delete Fence，并依据 point ID 或内容哈希
  将过期操作标记为 `SUPERSEDED`。新增重现覆盖相同 point ID 与跨 point ID 的情况；
  后续新接收的写入仍可重新添加该事实。
- 为长期保留的 events/operations 历史增加 SQLite 索引；升级时通过
  `CREATE INDEX IF NOT EXISTS` 增量创建，不清理行或幂等键。100,000 条合成 pending
  operation 的空 ID 检查由未索引约 0.58 秒/100 次降至约 0.0003 秒/100 次。
- 最终测试、最新 Hermes 主线 SHA、未覆盖的服务/崩溃边界和发布建议以最终审查报告为准。
- PR #11 的代码实现提交 `a81d09c` 已通过 CI
  ([run 37766236440](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236440))、
  Linux/macOS/Windows × Python 3.11/3.14 平台矩阵
  ([run 37766236512](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236512))，
  以及最新 Hermes 跟踪检查
  ([run 37766274748](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766274748))。
  最新 Hermes 实际检出 SHA 为 `e6848c2c9a86e84d9a5c672085bb7d79079a66ba`，两个 Python
  版本均通过。Cloud smoke 运行
  ([37766278783](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766278783))
  在测试开始前被 `qdrant-cloud` 环境保护规则拒绝；Cloud 行为尚未验证。
- CodeRabbit 状态虽为绿色，但其报告要求 OSS 仓库人工审查；Sourcery 检查已跳过，
  不将这两项表述为已完成的自动化代码审查。当时的预发布决定为 **NOT READY**。

## 历史 PR 摘要（PR #10，已合并）

验证日期：2026-10-08

仓库基线：`5281dc6fdb3c4fcb952cb67d6d1eb7cf3a31e870`（`main`）

Hermes 当前上游基线：`4787e4d56fc8d9265d4c7d3c0fe5accee86b4078`

## 基线

- 插件已有事件/操作 SQLite ledger、Qdrant 写入确认、重启重放、按目的地隔离、迁移恢复与作用域校验。
- 预审发现清空集合的 delete/create 窗口没有持久化恢复意图；删除后仍可能由先前已接纳但尚未准备操作的事件恢复记忆。
- 已提交的事件和操作长期保留原始 payload；异常退出前尚未进入插件回调的 Hermes 内存队列不在插件 ledger 的保证范围内。
- 文件锁只协调本机合作进程，不能充当跨机器分布式锁。
- CI 尚未提供 Linux/macOS/Windows × Python 3.11/3.14 的直接行为契约矩阵，也没有跟踪每周 Hermes 最新 `main` 的独立作业。检索基准样本较小。

## 发现

1. **重置恢复（高）**：在删除 Qdrant collection 后、重建并校验身份前进程退出，会留下数据状态和本地账本不一致的窗口；不应依赖人工猜测恢复步骤。
2. **删除顺序（高）**：显式删除某条记忆后，较早接纳但尚未产生操作的事件可能再次写入同一内容，包括由不同来源生成的 point ID。
3. **保留与隐私（中）**：终态 ledger payload 可持续保留原文；同时，SQLite WAL、旧页和备份意味着逻辑清理不能被描述为安全擦除。
4. **Host/多进程边界（中）**：Hermes 在调用 `sync_turn` 前使用内存队列；本地 WriterLease 不能阻止另一台机器写同一共享目的地。
5. **分发与兼容性证据（中）**：manifest 元数据、跨平台覆盖、最新上游跟踪和检索指标的证据需要补全。

## 实施

- 添加目的地级 reset intent；`init` 可恢复中断重置，其他命令在存在待恢复 intent 时失败关闭。重置先重建并验证 Qdrant，再清除本地目的地账本；其他目的地及隔离会话保持不变。嵌入式 Qdrant 删除 collection 后关闭并重开本地 client，再确认删除完成。添加进程退出和各失败点注入测试。
- DELETE 操作持久化作用域、删除时间和内容 hash。重放会拦截删除前已接纳的旧事件，即使它会映射到不同 point ID；新接纳事件仍可有意重新添加该内容。
- 在账本打开、终态操作完成和迁移清单更新时，对 COMMITTED/SUPERSEDED payload 做逻辑清除。保留 PENDING/FAILED payload、去重与身份状态，以及未完成迁移恢复所引用的数据。
- 关闭 provider 初始化失败时已创建的 Qdrant 资源，避免泄漏嵌入式文件锁。
- 增加 Linux/macOS/Windows × Python 3.11/3.14 行为矩阵，以及每周和手动运行的 Hermes 最新 `main` 兼容性作业；后者记录实际 Hermes SHA。固定兼容性基线继续使用不可变 refs。
- 跨平台作业为测试子进程设置 Hermes host import path。只依赖 POSIX Bash/closed-pipe 行为的断言在 Windows 上单独跳过；POSIX 文件 mode 断言也只在 POSIX 上执行。
- 扩展合成检索样本并输出 Recall@1/5/10、Precision@1/5、MRR 及分类指标，不改排序算法。
- 两份 v2 manifest 补充 `author`、`license`、`homepage`、`tags` 和与 `pyproject.toml` 同步的 `python_dependencies`。`provides_hooks: []` 是有意设置：Hermes 此字段表示通用事件总线注册；本插件实现的是 `MemoryProvider` 回调，不调用 `PluginContext.register_hook`。完整 provider 回调清单和接口区别已写入架构文档并由真实 Hermes parser 合约测试覆盖。
- `.gitignore` 添加 `IDEA.md`；该文件不在本分支 Git 跟踪列表中。
- README（英文/中文）、架构、运维、安全、验证和 CI 文档说明重置恢复、删除屏障、保留语义、Host 队列边界及锁范围。

## 验证

| 检查 | 结果 |
| --- | --- |
| Hermes canonical runner，插件 `tests/` | 27 个文件；205 通过，0 失败，3 跳过 |
| Ruff `format --check` 与 `check` | 通过（Ruff 0.15.1） |
| `actionlint .github/workflows/*.yml` | 通过 |
| workflow YAML 解析 | 所有 workflow 解析通过 |
| wheel 构建及安装后 smoke | 通过：入口发现、包资源、schema 持久化和 CLI |
| 上游环境 | 使用 Hermes `4787e4d56fc8d9265d4c7d3c0fe5accee86b4078` 完成安装后 wheel 验证 |
| 合成检索评估 | 21 条记忆、24 条查询；Recall@1 `0.806`、Recall@5 `0.986`、Recall@10 `1.000`、Precision@1 `0.875`、Precision@5 `0.225`、MRR `0.931` |

3 项跳过与外部服务有关：真实 Qdrant Server 需要显式测试 URL；Cloud smoke 需要仓库凭据。合成检索数据很小，且 `privacy-lifecycle` 分类的 MRR 为 `0.333`；这些数字不能当作生产召回率保证。

## PR 摘要

- 标题：`fix: harden reset and ledger recovery contracts`
- 目标分支：`main`；源分支：`codex/pre-release-engineering-hardening`
- PR：[#10 — fix: harden reset and ledger recovery contracts](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/10)
- 历史状态：PR #10 于 2026-10-08 合并。该阶段的发布建议仅适用于当时审查的提交范围；本轮状态见上方最终加固记录和独立的最终审查报告。
- 本地验证结果与 GitHub 检查分开记录；后者不会被本地结果替代。

## 剩余风险

- Hermes 的 `sync_turn` 回调在到达插件并提交 ledger 之前仍处在 Host 内存队列中；Host 异常退出或有限等待的 shutdown 可能丢失尚未接纳的回调。插件无法在自身代码内使这段 Host 队列持久化。
- WriterLease 只保护同机合作进程。对共享目录或远端 Qdrant，维护操作前仍需由运维方停止所有主机上的写入者。
- payload 清理和 destination reset 是 SQLite 逻辑操作，不会保证覆盖 WAL/数据库旧页、快照或备份；不要宣传安全擦除。
- POSIX state/config mode 保护不会自动转化为 Windows ACL；Windows 操作方须在 Hermes profile 目录设置合适的 ACL。
- 插件侧完整测试没有连接外部 Qdrant Server 或 Cloud。平台矩阵和最新上游跟踪 workflow 已添加，但其 GitHub 运行结果仍须以 PR SHA 的实际检查为准。
- 评估集仍是小型合成数据；privacy-lifecycle 分类的首位排序表现需要更多标注样本验证。本次不调整检索排序算法。

## 历史发布建议（2026-10-06）

**READY FOR LIMITED TECHNICAL PREVIEW**

只建议面向知情的技术预览用户，并明确告知“持久化保证从插件回调写入 SQLite ledger 后开始”。不要将当前版本描述为覆盖 Hermes 入队到回调期间的无损 durable memory，也不要据此宣布 public beta 或发布版本。合并前还应查看草稿 PR 对应 SHA 的全部 GitHub 检查；本报告不会替代那些远端结果。
