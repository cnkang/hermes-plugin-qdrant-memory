# PR #63 检查、修复与合并判断

日期：2026-10-20。对象：[PR #63](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/63)。

## 结论

**建议保留并优先合并这个独立基础 PR，但当前暂缓合并，等待最新提交的必要 CI 和评审状态更新。**
本次没有执行 merge、auto-merge、发布或修改分支保护。

main `80816294611c1c0b896784cc12f86e72115699c6` 尚无 `shared_reader.py`。大研发分支
虽然已包含相同 Reader 实现，但它尚未进入 main，因此 #63 不属于重复或应关闭的 PR。
Reader 是 Native 共享召回的前置能力，单独审查和落地比合并整个研发分支更合适。

## 现场状态与修复

PR 先前被更新/重放至 `5f2f179cbb8df084f465948d80b7e6754ee01e73`，已包含当前 main；
没有沿用上轮记录的旧 head 作判断。本次修复已正常 fast-forward 推送至原 PR 分支，
新 head 为 `672952d231d6f161dea5231d6fa30adf3fee3239`，没有 force-push。

1. CodeRabbit 唯一未解决意见有效：Python 3.11 解析约 10 KB 的深层 JSON 会抛出
   `RecursionError`，超过客户端约定的脱敏 `ValueError` 边界。先复现失败测试，再让
   JSON 解析同时规范化这两类异常。Python 3.14 可解析该嵌套数组，仍按非对象 envelope 拒绝。
2. 人工复查发现 URL 解析器会回显畸形 IPv6/Unicode authority；两个新增用例先 FAIL，
   再将标准库解析失败转换为统一脱敏错误。没有放宽 loopback、端口、URL 或认证限制。

对应评审线程已标记解决，PR 描述已同步真实测试结果和非 Draft 状态。本次没有以
resolve thread 代替新批准，也没有撤销或伪造 CodeRabbit 的旧 CHANGES_REQUESTED review。
修复另已同步到 `codex/universal-owner`（`9aaff09`），避免后续集成丢失。

## 验证证据

| 验证 | 结果 |
|---|---|
| Python 3.14.7 + 实际 Hermes checkout 完整回归 | 605 PASS / 7 SKIPPED / 1 已知 warning |
| Python 3.11.15 / SDK 2.x standalone | 128 PASS |
| Reader + shared security 专项 | 83 PASS |
| 同步到 universal-owner 后的 Reader / Native Owner / shared recall 回归 | 50 PASS |
| Ruff check / format | PASS |
| CodeRabbit 本次四个修改文件的本地复查 | completed，0 findings |

七个跳过项依赖真实模型缓存、外部 TLS/Server/Cloud 测试环境；不代表已执行这些验收。
已知 warning 来自旧测试在 Embedded Qdrant 上模拟后端 alias 时创建 payload index。
CodeRabbit 的 0 findings 只覆盖本次四文件补丁，不是对整个系统的安全认证。

可复现：

```sh
PYTHONPATH=/path/to/hermes-agent .venv/bin/python -m pytest -q
.scratch/sdk2/bin/python -m pytest tests/standalone -q
.scratch/sdk2/bin/python -m pytest tests/standalone/test_shared_reader.py tests/security/test_shared_security.py -q
```

## 合并门禁与后续顺序

结束核验时，新 head 仍显示 `BLOCKED` / `CHANGES_REQUESTED`；没有已完成失败的 check，
但多项仍排队/运行。必须以 **672952d** 的最终结果为准，不能使用旧 head 的绿灯。
分支保护要求 `Required security scan gate`，严格要求跟上 base；该 gate 依赖完整 CI
下游任务，目前尚未产生可用于合并的成功结果。没有删除、跳过或模拟这个 gate。

- [本次 CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39296958034)
- [本次共享安全检查](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39296958063)
- [本次 standalone 矩阵](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39296957943)

在必要 CI 成功、有效评审意见全部关闭且评审状态更新后，建议维护者合并 #63。
此 PR 不会自动启用 Native 共享、自动提取或公网服务；这些继续按后续独立 PR 审查。
合并之后重新整理 universal-owner 的后续提交，避免重放已有 Reader；PR #64 的依赖
更新独立验证，不作为绕过本 PR 门禁的理由。
