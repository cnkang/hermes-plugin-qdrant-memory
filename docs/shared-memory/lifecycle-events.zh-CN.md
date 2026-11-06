# Owner 生命周期事件控制

可选生命周期扩展支持完整轮次 `turn_completed` 输入，以及设备绑定的状态查询和取消。四个 MCP 记忆工具保持不变。启用生命周期前，必须显式开启配置并使用受保护的 HTTPS Gateway；loopback JSON memory API 不是公开事件 API。

宿主适配器向 `POST /events` 提交稳定事件 ID 和获准的用户/助手内容。经过认证的 Owner 负责绑定身份、空间和捕获规则。`POST /events/manage` 只接受 `action`（`status` 或 `cancel`）及 `event_id`，沿用认证、Host/Origin 限制、速率/并发/请求体限制和 Runtime 锁。payload 不能指定 owner、device、agent 或 scope。

## 版本化候选输入

`turn_completed` 事件可以使用 `qdrant-memory/extraction-v1` 候选契约。宿主先用自身模型抽取候选，再把候选交给 Owner；不会把原始完整对话写入候选事件，也不会要求 Owner 再抽取一次：

```json
{
  "event_id": "dsh-native-<sha256>",
  "session_id": "dsh-<sha256>",
  "kind": "turn_completed",
  "candidate_schema": "qdrant-memory/extraction-v1",
  "candidates": [{
    "text": "项目使用现有 Runtime。",
    "category": ["project"],
    "importance": 0.8,
    "replacement_intent": false
  }]
}
```

每条候选只能包含 `text`、`category`、`importance`、`replacement_intent` 四个字段；每批最多 32 条。`text` 最多 4,096 字符；最多八个类别，每个类别最多 64 字符。`importance` 必须是 0 到 1 之间的有限数值。未知字段、无效类型、类似凭证的内容和超长 payload 都会被拒绝。需要替换旧事实时，可附带一段最多 2,048 字符的原始用户文本片段 `replacement_context`。Owner 仅在关系审查期间保留该片段，不把它写入持久化事件计划。

Owner 仍会验证授权、从 grant 推导 scope、运行现有关系审查与去重、持久化事件和 receipt，并通过既有 mutation/recovery 流程执行写入。宿主提供的候选不是可信写入指令。重试必须复用稳定事件 ID；只有取得 Owner receipt 后，客户端才可确认捕获完成。四个公开 MCP 工具的 schema 不变。该候选契约目前仅存在于源码预览；配套 Runtime 尚未发布前，已发布的 Runtime 不支持此契约。

## 状态、重试与取消

默认的 `remote-event --action submit` 保持向后兼容。状态结果只包含 `event_id` 和状态，不暴露原始记忆、抽取候选或 Provider 错误。未确认的传输中断不能证明 Owner 拒绝了事件；遇到可重试失败时应使用同一事件 ID 重发。`QUARANTINED` 对客户端是终态，只有运维恢复后才可重发。

取消是保守操作：只有尚无抽取候选或 operation plan 的原始 `PENDING`、`FAILED` 或 `QUARANTINED` 事件可以取消。已准备、已提交或结果不确定的写入不能取消；必须通过现有恢复/CAS 语义核对。取消不会回滚 Qdrant，也不会自动清除既有 Owner recovery gate。

## 适用范围

本扩展的授权、scope、事件收据、恢复和回放由 Python Runtime 管理。TypeScript 宿主适配器不应添加第二套 embedding、ledger、去重或存储写入实现。自动捕获不得承诺零丢失，除非宿主日志与 Owner receipt 的持久化边界已实际验证。确定性测试不代表模型准确性或公开部署已就绪。
