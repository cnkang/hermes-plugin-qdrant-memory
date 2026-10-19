# 后续任务推进报告

日期：2026-10-20。对应上一轮未完成事项核验；本轮已实际修改、测试并推送三个仓库。
没有合并 PR、发布版本、使用生产/Cloud 凭据、部署公网服务或迁移真实数据。

## 已推进的代码

1. **Runtime CI 根因修复**。URL 校验新增显式 `require_tls` 参数，移除假 API key 占位符；
   匿名的外部提取端点也必须 HTTPS。本机例外不变。Native 召回预算用例不再以线程
   80 ms 调度与 200 ms 截止时间碰运气，而是验证真实 Provider 路径传入的等待预算。
   网络启动重试与业务断言分离，避免吞掉断言失败。
2. **可恢复事件查询／取消**。新增设备绑定的 `/events/manage` 和
   `remote-event --action status|cancel`，仍只保留四个 MCP 记忆 CRUD 工具。
   原始事件取消事务带状态/正文 CAS，拒绝已准备、已提交或结果不确定的写入；
   同时清除原始事件及 admission plan，保留不可变输入摘要。重启与重试不能复活取消。
   撤权、跨设备、客户端伪造 scope 均拒绝；取消不清除现有恢复门禁。
3. **Pi 取消终态**。outbox 将 `CANCELLED` 与 `COMMITTED` 分开记录，取消后停止重放，
   不把取消包装成写入成功。MCP-to-MCP/事件客户端继续校验证书、禁止重定向及环境代理。
   管理错误仅保留有限机器错误类型和 retryability，不反射远端正文或错误消息。
4. **模型失败修复**。真实本机诊断证明原 800 token 限额耗尽于 reasoning，返回
   `finish_reason=length`、正文为空。Owner 增加显式有界 `max_output_tokens`（默认 800，
   最大 8192），对截断结果拒绝确认；增强临时状态与引用指令的提取约束。
   诊断脚本即使没有 provider exception，也会在案例语义失败时非零退出。
5. **Cloud L2 代码接入**。既有 `cloud-smoke.yml` 新增独立的 scoped Owner lane，
   要求预先授权配置的 disposable collection 和 collection-scoped key；不创建/删除
   collection，不回退使用管理员 key，仅清理本次已知合成记录 ID。
   覆盖跨 Agent project、same-agent private、不同 Agent 隔离、CAS、Ledger 重开回执。
   缺少凭据时，真实测试 job 为 SKIPPED，而不是仅在绿色 job 内打印跳过。
6. **部署与文档**。新增独立系统账户、0077 umask 的 systemd 样例及一致性备份/回滚说明；
   补齐 event controls、Cloud fixture 预置及新提取预算文档。Canonical Skill 同步预览边界。

实现仍使用同一个 Owner、Runtime、Extractor、Ledger 和现有 CRUD；没有新增记忆引擎，
没有让 Agent 绕过 Owner 写 Qdrant。没有改变版本号、正式 Runtime pin 或默认 private/local 模式。

## 测试与评审

多数新行为先复现失败，再改实现：强制 TLS API、截断结果、非法输出预算、事件管理 API、
原子取消检测、Pi 取消重放和错误正文脱敏均有从 FAIL 到 PASS 的证据。

- Runtime 最终全量：**678 PASS / 9 SKIPPED / 1 已知 warning**，总体 coverage **90%**；
  九个条件跳过包含新 Cloud lane，未替它申请或使用凭据。
- SDK2 最终 standalone 全量：**174 PASS / 0 SKIPPED**；最后 CAS/脱敏修复专项 **30 PASS**。
- MCP 分发集合：**144 PASS / 0 SKIPPED**；源码 Host 验收仍在独立零跳过 lane 必跑。
- Node Pi 合同与实际 Pi session：**7 PASS**。
- 最后 Runtime 源码的 Hermes→独立 Pi 进程、verified HTTPS 交接：**1 PASS**，
  使用实际宿主生命周期及 deterministic 模型/embedding，不是生产模型或 UI 验收。
- Ruff check/format、actionlint、diff check：PASS。systemd 样例通过真实 CLI parser 验证；
  没有执行真实 systemd 安装／回滚，不标为部署已验收。
- Cloud 新 lane 本地 **SKIPPED**：未取得或使用云凭据，不冒充 Cloud 已跑。
- Skill package validator：PASS。

CodeRabbit 两轮辅助复查共提出四项有效改进：umask 清理、管理错误类型、Cloud job
跳过可见性、取消事务 CAS。已逐项验证并修复。当前 Runtime 内所有 `process_event`
均在 Runtime lock 下运行，取消 CAS 额外防止 Ledger 被其他调用者更新后仍擦除计划。
受控交错的失败测试验证了该防线；不以取消恢复门禁解决问题。

## 真实模型诊断：本轮 pilot PASS，仍不是 L3 全面完成

模型仍为本机 `qwen3:8b`，没有下载新模型或访问付费端点。固定匿名 10 案例，
temperature 0、输出预算 2048：precision **1.0**，positive case recall **1.0**，
case success **1.0**，provider failures **0**；p50 **7207 ms**，p95 **25772 ms**。
诊断退出码 0。先前 0.8571/0.8 和 JSON 解析失败属于真实失败历史，没有删除负例。
本轮核对模型 digest 仍为 `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`。

- dataset SHA256：`2e75f289a9fb44822db92a079a47afc4268bc3835298b1f59644b67498f8c74a`
- prompt code SHA256：`5cae974f656ca5a50547253972512604daea138bd66afb2173d66cf766cbddba`
- schema SHA256：`fefe6d2d6a321b8ff716c32df07e4527f8779ffdfe52065bfb25e627dbbedd3c`

这只是 extraction-only pilot。大规模固定多轮数据、跨 Agent 召回成功率、错误注入率、
完整并发/恢复统计以及 RC/周期质量门禁仍未完成，不能由小样本 100% 推导生产准确率。

## 远端证据与精确状态

- [MCP 分发矩阵 4d45b15](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39339088406)：
  **SUCCESS**，核对全部 12 个 OS/Python/SDK 组合均成功，原分发零跳过故障已得到远端修复证据。
- [MCP cross-harness 4d45b15](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39339088351)：SUCCESS。
- [Runtime platform 5be9837](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39339244092)：
  **SUCCESS**，包含之前失败的 macOS/Python 3.14。
- [Runtime CI 5be9837](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39339244303)：
  Snyk code **SUCCESS**；Sonar 的 reliability/security 均恢复 rating 1、BUG/VULNERABILITY 为 0，
  但该旧快照新代码 coverage 72.5% 仍 FAIL。之后增加的生产路径测试不借用旧结果宣称闭环。
- [Runtime security e33f7ae](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39340970088)：
  **SUCCESS**；SDK1/SDK2 双网络 HTTPS、protected TLS/JWT、真实 Server 设备身份均实际通过。
- 最终功能快照 `aa9565de5d0cd8f09e64c2a66e0858505b2b7dac` 的
  [CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532896)、
  [platform](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532944)
  仍排队；[security](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532898)
  已 SUCCESS，optional embedding 也 SUCCESS。不能用本地总体覆盖率或安全矩阵绿灯替代尚未完成的 CI/Sonar。
- MCP 最新 `cce1632` 将源码 Host lane 固定至上述完整 Runtime SHA；
  [distribution](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39341606084) 与
  [cross-harness](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39341605787) 为最终待确认 run。

## 剩余工作与下一步

1. 收齐最终 SHA 的 CI 和 Sonar ≥80% 新代码覆盖率结果；若仍失败，按真实缺口修复。
   原 42 个 Sonar issues 并非全部漏洞；复杂度/测试风格等既有余项没有通过关闭规则清除。
2. 已合并 #63；其余功能仍在研发分支，按 Native recall、身份、Transport、Owner/lifecycle
   拆分可审查 PR。依赖 #64 独立处理，不合并到功能提交掩盖验证范围。
3. 新 Cloud 测试已写入 workflow，但真实 scoped Cloud 运行需显式授权与 disposable
   配置；完整 Host UI、公网部署、服务监管/升级回滚实际演练仍 UNVERIFIED。
4. OAuth/OIDC、Protected Resource Metadata、发现/PKCE 和交互客户端模式仍未实现，
   按任务书允许的独立阶段推进。当前仅预配置静态服务凭据模式。
5. 完整 L3 多轮质量矩阵、Native 远程自动客户端及更多官方 Host 自动 adapters 仍为后续工作；
   当前 Hermes+Pi 达到最低自动宿主范围，其他宿主继续 explicit MCP/assisted。
6. 正式发行仍待授权：Runtime 新版本 → MCP 正式依赖/Managed lockfile → Skill。

新增操作说明：[事件管理](../shared-memory/lifecycle-events.md)、
[Cloud Owner 验收](../shared-memory/cloud-owner-acceptance.md)、
[服务安装与回滚](../shared-memory/service-installation.md)。

源码推送快照：Runtime `aa9565d`、MCP `cce1632`、Skill `fc1209b`。
本报告及差距快照索引本地提交保存，避免仅为同步报告再次中断最终功能 SHA 的在途 CI。
