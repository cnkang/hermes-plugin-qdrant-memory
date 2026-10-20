# Proposed maintenance release notes (unpublished)

Recommended Runtime version: **0.4.1**, subject to review and required CI. Package
metadata remains 0.4.0 until release preparation. No tag or publication is part
of this change.

## English

- Add optional trusted Shared Owner destination policy binding backend,
  canonical destination, collection, schema, embedding fingerprint and profile.
- Reject conflicting cooperating local profiles with durable account-local
  claims and OS locks. Retain the existing profile writer lease.
- Reject unsafe protected Owner state before recovery and write admission.
- Add non-destructive `shared-doctor --policy` JSON diagnostics with stable
  PASS / FAIL / UNVERIFIED results and exit codes 0 / 1 / 2.
- Document Owner-only Qdrant credentials, coherent backups, local/Server/Cloud
  deployments and NAS/container limits. Add negative ownership, recovery,
  disclosure and real TLS/JWT database authorization tests.

Private workflows and legacy grants remain supported. After explicit enrollment,
ordinary private/provider/maintenance writers to the protected destination are
rejected. Deliberate transitions require coordinated offline operations. Protected
mode requires verified POSIX permissions. It is local admission, not distributed
fencing: direct writes with independent database credentials bypass Ledger and
application CAS. Key scope and administrator access remain deployment concerns.
No memory payload, shared protocol or Ledger migration is required.

Authenticated cross-container transport changes the trust boundary and is
deferred to a separate **0.5.0** design. Loopback binding remains unchanged.

## 中文

- 新增显式启用的 Shared Owner 目的地策略，绑定后端、规范化目的地、集合、
  数据格式、嵌入指纹及 Owner profile。
- 通过同一系统账户下的持久目的地声明及操作系统锁，拒绝配合协议的冲突本地
  profile；保留原有 profile writer lease。
- 在恢复与写入准入前拒绝权限不安全的受保护 Owner 状态。
- 新增只读 `shared-doctor --policy`，输出稳定的 JSON 检查项，区分
  PASS、FAIL、UNVERIFIED，退出码分别为 0、1、2。
- 补充 Owner 独占数据库应用写凭据、一致性备份、各类部署及容器传输限制说明，
  新增所有权、恢复、信息披露和真实 TLS/JWT 数据库授权的负向测试。

原私有流程与旧授权策略继续兼容。目的地一旦显式登记保护，普通私有、原生 provider
和维护写入入口将拒绝访问该目的地；有意切换须停写并执行明确的离线操作。
新保护模式目前要求可验证的 POSIX 权限。它不是分布式 fencing；独立数据库写凭据
仍可绕过 Ledger 和应用层 CAS，凭据权限与管理员访问必须由部署方管理。
无需更改记忆 payload、共享协议或迁移 Ledger。

跨容器认证传输应另行设计为 **0.5.0**，本次保持 loopback 绑定。建议版本为
**0.4.1**，仍需代码审查与必要 CI；此次不修改版本元数据、不创建 tag、不发布包。
