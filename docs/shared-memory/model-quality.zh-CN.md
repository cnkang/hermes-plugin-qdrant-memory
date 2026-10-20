# 可选的真实模型生命周期质量评估

[English](model-quality.md) | [简体中文](model-quality.zh-CN.md)

## 评估边界

此源码预览通过真实的 Hermes MemoryManager 回调、Pi SDK 会话及官方 HTTPS MCP 客户端评估现有 Owner Runtime。
Owner Runtime 控制记忆提取和写入。评估使用其现有提取与写入路径。
MCP 指 Model Context Protocol（模型上下文协议）。SDK 指软件开发工具包。

Pi assistant 对每轮给出固定确认。提取和向量化通过回环 HTTP 上的 Ollama 使用已安装的本地模型。
Embedding 将文本表示为数值向量。模型为 `qwen3:8b` 和 `qwen3-embedding:4b`。
评估不需要 API key、Cloud 服务或模型下载。

## 固定基线与质量门禁

固定匿名语料为 [lifecycle-v1.json](../../tests/quality/lifecycle-v1.json)。
其中包含跨 Hermes 私有空间、Pi 私有空间及显式授权项目空间的 24 轮对话。
语料包含 16 个标注正例事实、8 轮负例及 32 个检索查询。
概念组允许改述，无需另一个模型担任裁判。报告记录精确的语料和模型摘要。
此小型合成语料用于衡量回归，不估计生产准确率，也不认证用户界面。

门禁要求以下最低分数：

| 指标 | 含义 | 最低值 |
| --- | --- | --- |
| Precision | 正确提取事实数除以提取事实数 | 0.95 |
| Positive-case recall | 提取出正确事实的正例数除以正例总数 | 0.90 |
| Recall@5 | 在前五个搜索结果中找到预期事实的比例 | 0.90 |
| MRR@5 | 前五个结果内首个正确结果排名倒数的均值 | 0.80 |

门禁还要求作用域泄漏为零、provider 失败为零、重放安全，以及 Owner 重启后持久化保持。
缺少观测会使门禁失败。重启后，评估器重新执行全部 32 个查询。
记录 ID 和返回的每个 payload 字段必须相同。
比较排除计算出的相似度分数；评估器重新评分排名，并保留原始比较诊断。
事件回执必须保持不变，重放不得再次调用模型。

延迟摘要报告检索与模型调用的实测 p50 和最近秩 p95。
这些百分位分别表示中位数和第 95 百分位。

## 运行评估

使用 Python 3.11。
安装 `.[remote]`、Hermes 测试依赖及 MCP checkout 中锁定的 Pi 依赖。
从 Runtime checkout 运行以下命令：

```sh
PYTHONPATH="$PWD:/path/to/hermes-agent" python scripts/evaluate_owner_lifecycle.py \
  --adapter-root /path/to/qdrant-memory-mcp --output /tmp/owner-quality-new.json
```

使用新的输出路径。
每次运行创建私有临时 home、合成凭据及临时 Embedded Qdrant 数据。
运行结束后关闭这些资源并移除临时数据。报告包含指标和哈希，不包含 token 或记忆正文。

失败返回非零退出状态，并保留 FAIL 记录。
观测与记录错误和模型 provider 失败分别统计；两者都会使门禁失败。
重启和回执重放不得增加模型调用次数。
模型发现或初始化失败也会生成脱敏的 FAIL 产物。

## 已提交的证据

| 证据 | 含义 |
| --- | --- |
| [owner-model-quality-evidence.json](owner-model-quality-evidence.json) | 评估器复审修复前的基线。原始重启差异仅涉及计算出的分数。 |
| [owner-model-quality-reviewed-evidence.json](owner-model-quality-reviewed-evidence.json) | 评估器修复后的运行，包含明确的重启后模型调用和记录错误观测。 |
| [owner-model-quality-drained-evidence.json](owner-model-quality-drained-evidence.json) | 先完成公共 Host 准入屏障，再完成 Provider 空闲屏障后的新基线 PASS。 |
| [owner-mutation-initial-failure.json](owner-mutation-initial-failure.json) | 首次完整基线与 mutation 运行。在替换意图、Host 准入屏障和拒绝探针修复前记录 FAIL。 |
| [owner-mutation-quality-evidence.json](owner-mutation-quality-evidence.json) | 历史原始 PASS。因读写凭据均属于 `pi`，跨 Agent 交接结论已撤回。 |
| [owner-mutation-identity-evidence.json](owner-mutation-identity-evidence.json) | 新的完整基线与 mutation PASS，存储的写入者为 `pi`，独立授权的读取者为 `hermes`。 |
| [owner-model-context-evidence.json](owner-model-context-evidence.json) | 首次观测真实 Pi 模型输入的完整运行。 |
| [owner-model-context-reviewed-evidence.json](owner-model-context-reviewed-evidence.json) | 上下文评估器复审后的完整重跑。测量了 12 次真实 Pi 模型输入和 38 条注入记忆。 |
| [owner-model-stack-evidence.json](owner-model-stack-evidence.json) | 新的完整真实模型 PASS，包含被评估栈的源文件指纹。 |

排空后的基线通过真实本地模型执行全部 24 次捕获、32 个查询、重放检查及重启检查。
Host 准入屏障确认回调已交付；Provider 空闲屏障确认已准入工作完成。
首次 mutation 失败记录早于 `baseline_executed` 和 `evaluation_mode` 字段。
它不能作为当前候选的通过证据。历史 mutation PASS 保留用于追溯，不代表当前验收通过。
其 `superseded` 和 `withdrawn_conclusions` 字段标记已撤回的结论。原始测量保持不变。

修订后的评分器通过存储的来源信息和实际读取授权检查不同的逻辑 Agent 身份。
身份缺失或相同会使门禁失败。
身份验证运行通过全部十一项检查，8 个观测命中中有 0 个过时命中。
并发 compare-and-swap（CAS，比较并交换）产生一次成功更新和一次冲突。重启、重放及撤销检查也全部通过。
CAS 仅在提供的版本与存储版本相同时接受更新。

首次上下文观测运行测量了 12 次真实 Pi 模型输入和 38 条注入记忆。
标注错误与作用域违规均为零。所有基线和 mutation 门禁也均已通过。

栈验证运行覆盖 24 轮基线及 32 个查询。
Precision、positive-case recall、Recall@5 和 MRR@5 均为 1.0。全部十一项 mutation 检查通过。
它测量了 12 次真实 Pi 模型输入和 38 条注入记忆，错误注入与作用域违规均为零。
重启新增模型调用为零。
源文件指纹将证据绑定到被评估文件，不认证 Git 提交、生产准确率或远端 CI 执行。

## 驱动契约

`pi-quality-driver.yml` 在标准 GitHub 托管 runner 上定义契约检查 job。
该 job 使用固定版本的 Pi SDK、真实扩展和合成 Owner 传输层。
它重复提示文本，并把部分交付延迟到后续 case。
每条 receipt 必须保留原始 event 与 case 的关联。

该 job 拒绝跳过或未执行测试的结果。
此工作流不会选择模型 runner。
它的实际结果需要已完成的 CI run。

## 手动工作流

`owner-model-quality.yml` 工作流使用带 `qdrant-memory-models` 标签的一次性 Linux 测试 runner。
操作员必须显式配置此 runner。工作流不由 pull request 或 fork 触发，也不使用生产凭据。
它检出不可变的 Hermes 和 MCP 候选版本，并要求模型已在本地安装。
仅有工作流配置不能证明已执行；注册 runner 和远端执行需要分别操作。
普通测试 CI 也运行评分器单元测试。

## 真实模型输入观测

Pi 质量驱动器在生命周期上下文 hook 执行后，通过 `streamSimple` 观测真实模型输入。
`context_quality` 结果单独衡量错误的标注记忆注入，不以提取 precision 或检索指标替代。
注入指将召回记忆加入模型输入。

案例观测缺失或重复会使门禁失败。
没有实测注入、出现未知事实、空间错误或 visibility 错误，也会使门禁失败。
读取凭据仅授予项目访问，包括将捕获写入 Pi 私有空间的轮次。
注入事实必须符合项目标注。此测量不评估不受限制的私有召回，也不评估生产事实准确率。
格式错误的记忆 JSON 会中止驱动器并生成脱敏 FAIL 产物，不能记为零错误。

## Mutation 与多语言扩展

增加 `--scenarios tests/quality/lifecycle-mutations-v1.json`，在基线捕获、检索及重启门禁后运行固定 mutation 语料。
不同会话中的中文 Hermes 轮次测量显式事实替换，以及同一逻辑 Agent 从第二台设备继续使用记忆。
事实替换指用较新的事实取代已有事实。
真实 Pi hook 捕获中文文档偏好及经授权的项目架构决定。

显式删除、重放及并发 CAS 均通过验证证书的 HTTPS MCP 执行。
两个独立认证的逻辑 Agent 并发更新同一个项目记忆版本。
门禁要求恰好一次更新和一次冲突。
项目只读授权必须拒绝删除，并在撤销后无法通过认证。
再次重启检查持久删除、成功更新的回执、撤销、保留事实，以及没有新增模型调用。
普通网络失败不能算作授权拒绝。

任何场景缺失或为假都会使扩展失败。
被替换的旧事实仍出现在实测的最新事实搜索中，也会使扩展失败。
`obsolete_hit_rate` 指标仅描述该标注查询。它不衡量生产幻觉或总体注入率。
基线和 mutation 语料分别保留版本及哈希。仅有基线证据不能证明扩展已执行。

手动模型工作流显式启用此扩展。单独记录其实际执行证据。
使用 `--scenarios-only` 单独运行 mutation。
报告注明 `evaluation_mode=mutations-only`、基线案例和查询均为零，以及 `baseline_executed=false`。
诊断成功时，顶层报告 `status=DIAGNOSTIC_PASS`，并保留 `mutation_scenarios.status=PASS`。
退出状态 0 仅表示诊断成功，不认证 v1 基线。
失败和异常均报告 `status=FAIL`，并返回退出状态 1。

完整运行成功时，顶层报告 `status=PASS`，且 `baseline_executed=true`。
手动工作流要求这两个字段及 mutation `PASS` 才能验收。
它不接受 `DIAGNOSTIC_PASS` 作为完整质量结果。

手动工作流还测试同一轮输入替换两个不同项目事实的情况。
它分别使用确定性接口和已安装的本地模型，运行精确记录 ID 更新契约。
模型探针使用关闭思考模式的 Ollama 原生测试 API，不改变生产模型路由。
工作流拒绝跳过模型探针，并保存其 JUnit 结果。

## 显式事实替换

`replacement_intent` 提取标志要求对同一授权作用域内最多五个已有候选进行关系复核。
复核可包含低于普通相似度阈值的候选。原始用户语句仅作为不可信证据。
只有事实层面的 `SUPERSEDES` 关系才能授权以更新代替新增。
新事实必须描述同一主体和属性，并给出替换值。
完整用户语句提供意图证据，不能使另一主体的事实替换当前候选。
首次关系检查不包含完整语句。只有 `CONFLICT` 结果才能请求带有意图证据的第二次检查。
此标志不能改变授权、目标作用域或写入权限。

新提取 schema 按[严格 Structured Outputs 契约](https://developers.openai.com/api/docs/guides/structured-outputs)要求提供每个属性。
旧的持久候选若缺少该标志或标志为假，继续采用此前复核策略。
Embedding 和存储的 payload schema 保持不变。一次显式替换可能需要最多十次关系模型调用。
报告实测延迟时必须包含这些调用。
