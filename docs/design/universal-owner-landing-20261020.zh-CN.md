# Universal Owner 验证记录

日期：2026-10-21。本文记录源码预览的验证范围，不代表正式发行版或生产环境验收。
英文功能说明见 [Remote Owner](../shared-memory/remote-owner.md)。
本文是独立验证记录，不是该英文指南的译本。

## 能力范围

| 能力 | 已验证范围 | 边界 |
|---|---|---|
| 本机共享 | 多个 Harness 通过独立授权访问同一单活 Owner | 客户端不能绕过 Owner 写入 Qdrant |
| 跨设备共享 | HTTPS MCP Streamable HTTP、设备独立凭据、同逻辑 Agent 延续、授权项目共享 | 当前使用预配置服务凭据，不支持 OAuth/OIDC |
| Hermes Native | 复用 Provider、Runtime、Ledger、worker 和宿主 LLM | 自动项目捕获需要明确授权规则 |
| 非 Hermes 自动记忆 | 实际 Pi 1.3.0 生命周期与上下文 hook | 其他 Harness 仍需显式 MCP 或辅助操作 |
| 后端 | 保留 Embedded、自托管 Qdrant、Qdrant Cloud | Cloud 专项验收需要独立凭据与环境 |
| 一致性 | Ledger、CAS、幂等、失败恢复、prepared 工作保护 | 单活 Owner，不提供分布式高可用 |

正式 Runtime 0.4.0 不包含这些源码预览能力。
加载 Skill 或注册 MCP 工具本身不提供自动记忆生命周期。

## 安全与恢复

Native Owner 不会把未准入输入重新归属给其他身份。
显式重试后，启动过程可以取消作用域不匹配的原始未准入输入。
取消状态、payload 清除和计数通过同一 CAS 事务提交。
重复启动不会重复计数。

已生成候选、操作键或 Owner authority 的工作不能通过此路径取消。
FAILED 工作仍需显式操作员重试。
清理逻辑会清除旧 CANCELLED 事件的原始 payload。
独立离线命令同样受写者租约和恢复保护约束。

客户端必须验证 TLS。
设备凭据与逻辑身份、空间、权限绑定。
撤权阻止新的授权访问，不会擦除已交付的模型上下文。
私有记忆与授权项目记忆使用不同权限范围。

## 测试记录

| 测试 | 结果 | 覆盖范围 |
|---|---|---|
| Native、Pillow 13 与 certifi 修复完整测试 | 732 PASS、9 SKIPPED | SDK 2.x，包含取消门禁、锁外授权与安全依赖回归 |
| 质量基线完整测试 | 755 PASS、9 SKIPPED | 包含 Pi 事件重放、延迟收据、依赖锁和缺失证据回归 |
| 质量扩展完整测试 | 791 PASS、10 SKIPPED | 包含上下文评分、变更场景、Pi 事件归属、替换关系和依赖锁回归 |
| Native 与维护定向测试 | 44 PASS | 自动取消、同作用域继续处理、prepared 保护、回滚与擦除 |
| 锁外授权定向测试 | 13 PASS | 真实 policy 加载屏障、身份与代次变更、数据变更、撤权 |
| Python 3.14 / SDK 1.x 相关测试 | 52 PASS | Native 授权与必需安全门禁 |
| 实际 Pi 测试 | 11 PASS | 真实 SDK 会话、UTF-8 分块、并发 drain |
| 实际 Hermes 到 Pi 交接 | 1 PASS | 固定版本宿主、确定性模型、验证证书的 HTTPS MCP |
| 实际模型完整评估 | PASS | 24 次基线捕获、32 次查询、11 项变更检查 |

完整测试的 SKIPPED 项需独立环境执行。其中实际模型关系回归已单独启用并通过。
测试保留了既有 Embedded payload-index 警告。
这些记录不替代最终提交的 GitHub CI。

实际模型证据见 [owner-model-stack-evidence.json](../shared-memory/owner-model-stack-evidence.json)。
最终复测证据包含 43 项源码 hash 和 Pi extension hash，均与对应实现匹配。
评估使用修复后的关系判断和 CAS 评分，完整基线、变更与上下文评估均为 PASS。
CAS 版本是无序令牌。替换评分要求同一记录的令牌发生变化，并且旧事实查询无命中。
四项基线质量指标均为 1.0。
实际 Pi 观察到 12 次模型输入和 38 条注入记忆。
标注错误与空间越权均为 0，重启没有增加模型调用。
并发 CAS 有一个成功更新和一个冲突，过时事实命中为 0/8。
这些指标仅适用于固定匿名语料，不估计生产准确率。

必需安全门禁在 workflow 取消后仍运行，取消的上游结果不能成为成功验收。
中英文启动示例均要求 policy 解析后的路径位于当前工作目录内。
Native 授权在 Provider 锁外读取 policy，交付前仍复查身份、代次、数据版本和撤权。
修复前 12 项锁屏障测试失败，修复后全部通过。
CodeRabbit 后续审查覆盖本轮 7 个修改文件，结果为 0 issues。
该结果不代表整个系统获得安全认证。

历史证据中的首次替换场景 FAIL 保留。
关系判断修复后的首次完整 FAIL 也保留，详见模型质量指南。
早期同逻辑 Agent 的交接结果不能用作跨 Agent 验收。
当前评分要求存储的写者与实际读者属于不同逻辑 Agent。

## 依赖兼容性

[PR #69](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/69) 已以 rebase 合并。
合并提交为 `21623289efd508a9ded7db0e73974fb3ce7e1a9b`。
合并前最终提交有 86 SUCCESS、1 Sourcery SKIPPED，无未解决审查线程。

[PR #74](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/74) 已以 rebase 合并。
被检查提交为 `591ee1e920514fdc863b7ab7e6011312242bbe07`。
该提交有 86 SUCCESS、1 Sourcery SKIPPED，无未解决审查线程。
合并提交为 `e8485747082e6ebe8aa3eaef7ca742923babcda5`。

临时 FastEmbed 构建仅调整分发元数据以接受 Pillow 13。
源码 wheel 与输出 wheel 均固定 SHA-256，推理源码和许可证不变。
CI 保留严格 hash、二进制包限制、依赖检查和安全扫描。
上游发布兼容版本后仍需重新验证，再删除临时构建。

[PR #76](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/76) 已合并。
合并提交为 `4d63c2067fea5a82dfdea573c1f215c21767e775`。
三份依赖锁文件使用 certifi 2026.10.19，保留 Pillow 13 的兼容构建和严格 hash 检查。

重放评估选择 COMMITTED 捕获，不能用早期 PENDING 回执作为比较基准。
Windows 中文语料读取显式使用 UTF-8，cp1252 默认编码模拟回归通过。

## 未验证范围

- [Cloud Owner main 测试](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39620288178)已通过 1 项端到端测试，0 跳过，并通过独立集合清理检查。测试使用测试专用 Key；集合级数据库凭据限制仍为 UNVERIFIED。
- 固定 Liquid 免费模型的[完整质量 CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624008695)在 main 上通过基线、mutation 与上下文检查。其他固定免费模型和 Free Router 的失败结果保留。历史本地 reference PASS 与本次 CI 分别记录；独立 self-hosted reference runner 仍为 UNVERIFIED。
- Schema 上下文修复前，较小模型存在空捕获，2B/4B 存在重要性校验失败。修复后，0.8B 和 4B 均通过五次独立生命周期、每次八项检查；0.6B 仍因项目空捕获失败。默认选择 0.8B，完整免费模型质量需用新适配器在 main 重测。详见[模型质量指南](../shared-memory/model-quality.zh-CN.md)。
- 公网部署、真实服务安装、完整用户界面验收保持 UNVERIFIED。
- OAuth/OIDC、交互授权及其他 Harness 自动生命周期不在当前已验证支持范围内。
- 当前源码预览不自动升级既有数据，不执行生产迁移。
