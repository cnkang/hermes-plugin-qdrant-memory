# 可选的真实模型生命周期质量评估

[English](model-quality.md) | [简体中文](model-quality.zh-CN.md)

## 评估边界

此源码预览通过真实的 Hermes MemoryManager 回调、Pi SDK 会话及官方 HTTPS MCP 客户端评估现有 Owner Runtime。
Owner Runtime 控制记忆提取和写入。评估使用其现有提取与写入路径。
MCP 指 Model Context Protocol（模型上下文协议）。SDK 指软件开发工具包。

Pi assistant 对每轮给出固定确认。提取和向量化使用所选模型配置。
Embedding 将文本表示为数值向量。
`reference` 配置通过回环 HTTP 上的 Ollama 使用已安装的 `qwen3:8b` 和 `qwen3-embedding:4b`。
此配置不需要 API key、Cloud 服务或模型下载。

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
| [owner-model-quality-reviewed-evidence.json](owner-model-quality-reviewed-evidence.json) | 评估器修复后较早的运行，包含明确的重启后模型调用和记录错误观测。 |
| [owner-model-quality-drained-evidence.json](owner-model-quality-drained-evidence.json) | 仅基线运行的 PASS，先完成公共 Host 准入屏障，再完成 Provider 空闲屏障。 |
| [owner-mutation-initial-failure.json](owner-mutation-initial-failure.json) | 首次完整基线与 mutation 运行。在替换意图、Host 准入屏障和拒绝探针修复前记录 FAIL。 |
| [owner-mutation-quality-evidence.json](owner-mutation-quality-evidence.json) | 历史原始 PASS。因读写凭据均属于 `pi`，跨 Agent 交接结论已撤回。 |
| [owner-mutation-identity-evidence.json](owner-mutation-identity-evidence.json) | 新的完整基线与 mutation PASS，存储的写入者为 `pi`，独立授权的读取者为 `hermes`。 |
| [owner-model-context-evidence.json](owner-model-context-evidence.json) | 首次观测真实 Pi 模型输入的完整运行。 |
| [owner-model-context-reviewed-evidence.json](owner-model-context-reviewed-evidence.json) | 上下文评估器复审后的完整重跑。测量了 12 次真实 Pi 模型输入和 38 条注入记忆。 |
| [owner-reviewed-policy-initial-failure.json](owner-reviewed-policy-initial-failure.json) | 事实对隔离后的首次完整运行。替换检查失败，基线与上下文检查通过。不能作为通过验收的证据。 |
| [owner-model-stack-evidence.json](owner-model-stack-evidence.json) | 新的完整真实模型 PASS，包含被评估栈的源文件指纹。 |

排空后的基线通过真实本地模型执行全部 24 次捕获、32 个查询、重放检查及重启检查。
Host 准入屏障确认回调已交付；Provider 空闲屏障确认已准入工作完成。

首次 mutation 失败记录早于 `baseline_executed` 和 `evaluation_mode` 字段。
它不能作为当前候选的通过证据。历史 mutation PASS 保留用于追溯，不代表当前验收通过。
其 `superseded` 和 `withdrawn_conclusions` 字段标记已撤回的结论。原始测量保持不变。

策略复审失败记录早于不透明 CAS 标识检查修复。此前评估器按大小比较 hash 字符串。
CAS 标识没有大小顺序。替换必须保持同一记录 ID、使用不同标识，并且没有过时结果。

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

## 小模型冒烟工作流

`owner-model-smoke.yml` 在相关 pull request 和 main 变更时运行，也可手动触发。
默认冒烟配置选择关闭思考后的 `qwen3.5:0.8b`，以及 384 维的 `all-minilm:22m` 嵌入模型。
两个模型清单均固定在配置中。CI 要求五次独立生命周期运行全部通过后才能验收。
较小的 `qwen3:0.6b` 安装包约 523 MB，但它在前两次冒烟尝试中均返回空提取而失败。
[显式偏好尝试](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39586687759) 未完成原生捕获检查。
0.8B 模型使用与 0.6B 尝试相同的偏好语料和有效性检查。
安装包体积不代表进程内存。0.8B 安装包还包含视觉组件。
[首次 0.8B 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39610058850) 通过了全部八项冒烟检查。
它发起两次生成请求，观测 p50 为 6.00 秒，p95 为 6.31 秒。
Ollama 报告生成模型加载 574,074,386 字节，嵌入模型加载 49,387,928 字节，GPU 内存为零。
这些是一次冒烟运行的模型加载观测，不是进程内存峰值或质量基准。
[后续 0.8B 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624059913) 因 Pi 项目捕获未返回记忆而失败。
两次生成请求均返回 HTTP 200。此前两次通过不能证明重复运行稳定性。
此前 0.8B 组合的模型下载合计约 1.37 GB。
[首次 1.7B 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39627174644) 同样因原生捕获为空而失败。
这些失败使用以 "Remember" 开头的命令式请求。
提取器排除命令并接受持久事实。命令解释是可能原因，但尚未验证。
独立的 `owner-smoke-declarative-v2` 语料直接陈述用户偏好和项目事实。
报告包含此版本及其 SHA-256 摘要。完整质量语料与有效性检查保持不变。
工作流复用相同初始化与缓存，不需要 API Key 或 self-hosted runner。
独立的 `model-smoke` Environment 不要求任何 Secret。

手动运行可通过 `model_profile` 选择 `smoke-qwen06`、`smoke-qwen08`、`smoke-qwen17`、`smoke-qwen2` 或 `smoke-qwen4`。
它们分别使用 `qwen3:0.6b`、`qwen3.5:0.8b`、`qwen3:1.7b`、`qwen3.5:2b` 和 `qwen3.5:4b`，嵌入模型和检查相同。
每次运行仅选择一个模型。Pull request 使用单一的 `smoke-ollama` 默认配置。
对此默认配置，可选的 `OLLAMA_SMOKE_PROFILE` Environment Variable 优先于同名 Secret。
值必须为上述五个配置之一或 `smoke-ollama`。无效值在检出或模型初始化前失败。
显式手动对比优先于此覆盖设置。仍要求五次重复全部通过。

冒烟测试使用真实模型调用、Hermes 原生捕获、Pi SDK 和 HTTPS MCP。
其两个合成对话轮次陈述用户偏好和项目事实。
具名主体提取、多语言语义和替换能力仍由完整质量语料评估。
它检查授权召回、私有隔离、重放，以及不新增生成调用的持久化重启。
它查询模型已捕获的文本来检查存储往返，不评估语义准确率。
空捕获、provider 失败、无效 payload、缺少观测或检查失败均产生 FAIL。
每次重复使用全新的 Owner 数据、设备凭据和宿主状态。Ollama 进程和固定权重保持预热。
任何一次重复失败都会停止运行，后续成功不能替代该失败。
报告在 `runs` 中保留每次已完成的重复，`requested_repetitions` 记录所需次数。
成功仅标记为 `SMOKE_PASS`，并设置 `quality_evaluated=false`、`baseline_executed=false`。
该结果不能认证完整质量基线或 mutation 门禁。

陈述句 v2 比较中，[0.6B](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39678861759)、[0.8B](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39678867271) 和 [1.7B](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39678857488) 均失败。
每次均在首个生命周期中因空捕获停止。HTTP 200 响应不能证明捕获成功。
[2B 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39680318405) 提取出原生记忆，但其重要性数值未通过 Runtime 校验。
Owner 阻止了写入。后续 4B 候选保留相同的 v2 语料、Prompt、嵌入模型和门禁。

[首次 4B 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39680955844) 也在完成生命周期前因重要性数值校验失败。
这些运行发生在 Owner 适配器将 Schema 加入模型上下文之前。
现在适配器除 `response_format` 外，还在系统消息中包含可信的 JSON Schema，遵循 [Ollama 结构化输出指南](https://docs.ollama.com/capabilities/structured-outputs)的建议。
这会改变模型的实际 Prompt。提取指令、语料、Schema 和 Runtime 校验保持完整。
此前质量与冒烟结果仅适用于其记录的源码指纹。需要新运行验证此适配器变更。

此适配器修复后，[0.8B 比较](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39682043203)在 `cffe602` 上完成五次独立生命周期，每次八项检查全部通过。
它发起十次生成调用，实际身份符合固定模型。模型摘要匹配，进程清理通过。
Ollama 报告生成模型加载 574,074,386 字节，嵌入模型加载 49,387,928 字节，GPU 字节数为零。
这些值描述已加载模型，不代表进程内存峰值。
[4B 比较](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39681606697)也通过五次生命周期，但生成模型加载 3,146,734,631 字节。
[0.6B 比较](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39681613565)仍因项目捕获为空而失败。
选择 0.8B 作为默认配置。其他配置保留为手动选项，CI 不运行模型矩阵。
实际 Prompt 已改变，因此此前质量结果不能认证新适配器。
位于 `24f9b11` 的 [main 分支对照运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39691877748)使用 Liquid Chat 和本地嵌入模型重跑了完整评估。
Precision、正例召回率、Recall@5 和 MRR@5 均为 1.0。Mutation 和上下文门禁通过，实际观察到 29 次 Chat 响应，provider 失败为零。
此对照不验证新的远程 Embedding 配置。

## 手动质量工作流

`owner-model-quality.yml` 中的质量模式仅由手动请求触发。
该模式不在 pull request 或 fork 上运行，也不使用生产凭据。
它检出不可变的 Hermes 和 MCP 候选版本。选择以下配置之一：

| 配置 | Runner | 生成 | 向量化 |
| --- | --- | --- | --- |
| `openrouter-free`（可选对照） | GitHub `ubuntu-24.04` | OpenRouter `liquid/lfm-2.5-2.6b:free` | `qwen3-embedding:0.6b`，1024 维 |
| `hosted-ollama`（可选对比） | GitHub `ubuntu-24.04` | `qwen3:1.7b` | 同一个本地 0.6B 模型 |
| `hosted-qwen35`（实验性） | GitHub `ubuntu-24.04` | `qwen3.5:2b` | 同一个本地 0.6B 模型 |
| `hosted-qwen35-4b`（实验性） | GitHub `ubuntu-24.04` | `qwen3.5:4b` | 同一个本地 0.6B 模型 |
| `reference` | 带 `qdrant-memory-models` 标签的一次性 Linux runner | 已安装的 `qwen3:8b` | 已安装的 `qwen3-embedding:4b`，2560 维 |

前四种配置不需要 self-hosted runner。
它们从 [Ollama](https://github.com/ollama/ollama/releases) 下载固定版本的 Ollama 和固定模型清单。
初始化检查 [model-profiles.json](../../tests/quality/model-profiles.json) 中的安装包校验和及模型摘要。
它在回环端口 19134 启动私有进程，并在测试后停止该进程。
CI 缓存依赖下载、固定的 Ollama 发布安装包，以及各配置的本地模型。
缓存键包含平台，以及对应依赖锁或模型配置的哈希。
每次恢复安装包缓存后，均先校验固定的 SHA-256，再解压或执行。
恢复缓存后也检查模型摘要和运行中的 Ollama 版本。
凭据、评估报告、进程 ID 和临时记忆数据均不纳入缓存。
模型工作流在质量检查前保存已校验的下载，因此后续质量失败不会丢弃这些缓存。
reference 配置保留端口 11434，并要求单独配置测试 runner。
reference 配置仅从 `main` 运行。
固定的缓存 Action 使用 6.1.0 版及 Node.js 24。reference runner 必须为 2.327.1 或更高版本。
初始化在测量前加载本地模型。报告的模型延迟不包含首次下载及加载。

## OpenRouter Cloud Smoke 与 Cloud Quality

这两个可选模式仅从 `main` 手动运行，不在 pull request 或 fork 上运行。
两者复用现有 Owner Runtime 和官方 API `https://openrouter.ai/api/v1`。
两者均不使用本地模型权重。`openrouter-free` 对照配置保留本地嵌入模型。

| 模式 | 配置 | 默认 Chat | 默认 Embedding | 预期维度 |
| --- | --- | --- | --- | --- |
| Cloud Smoke | `openrouter-cloud-smoke` | `liquid/lfm-2.5-2.6b:free` | `liquid/lfm-2.5-embedding-350m:free` | 1024 |
| Cloud Quality | `openrouter-cloud-quality` | `google/gemma-4-26b-a4b-it:free` | `nvidia/nemotron-3.5-embed-1b:free` | 2048 |

表中维度为预期值。实际批量响应必须在 Qdrant 启动前确认这些维度。
不匹配时运行失败。每次运行使用新的临时 Embedded Qdrant collection，并在测试后删除。
Cloud Smoke 通过 HTTPS MCP 执行一次合成 Hermes 和 Pi 生命周期。
它检查提取、向量、授权召回、私有 Agent 及用户隔离、重放和重启。
成功状态为 `SMOKE_PASS`，同时记录 `quality_evaluated=false` 和 `baseline_executed=false`。
Cloud Quality 使用完整固定语料、现有评分器、mutation 评估器和原有阈值。
HTTP 200、有效 JSON 或单条搜索结果均不能证明质量 PASS。

### 配置和运行

1. 打开仓库 Settings，再打开 Environments 中的 `model-quality`。
2. 添加测试 Secret `OPENROUTER_API_KEY`。不要将密钥放入 Variable、日志或 artifact。
3. 如需覆盖默认值，添加下表中的可选 Repository 或 Environment Variable。
4. 打开 Actions，选择“Optional real-model Owner lifecycle quality”，再选择“Run workflow”。
5. 选择 `main` 分支和所需配置。两个模型输入留空即可使用已配置默认值。

| 可选 Variable | 用途 |
| --- | --- |
| `OPENROUTER_SMOKE_CHAT_MODEL` | Cloud Smoke Chat |
| `OPENROUTER_SMOKE_EMBEDDING_MODEL` | Cloud Smoke Embedding |
| `OPENROUTER_QUALITY_CHAT_MODEL` | Cloud Quality 或对照 Chat |
| `OPENROUTER_QUALITY_EMBEDDING_MODEL` | Cloud Quality Embedding |

仅 API Key 为必需项，模型名称属于公开配置。
优先级为显式 `generation_model` 或 `embedding_model` 输入、对应 Variable、默认值。
对于质量 Chat，兼容的 `OPENROUTER_QUALITY_MODEL` Variable 或 Secret 位于新 Variable 之后、默认值之前。
空值视为未设置。四个 Variable 也可直接在本地 shell 中使用。
本地对应的手动输入为 `OPENROUTER_CHAT_MODEL_INPUT` 和 `OPENROUTER_EMBEDDING_MODEL_INPUT`。
需要归因对照时，以与 Cloud Quality 相同的显式 Chat 模型运行 `openrouter-free`。
这会保留现有本地嵌入模型。将结果与完整远程配置比较。
手动备选 Embedding 模型 `nvidia/nemotron-3-embed-1b:free` 必须通过显式覆盖选择。
不进行自动模型替换。

手动云运行缺少凭据时，会生成脱敏 FAIL artifact，并记录 `required_secret=OPENROUTER_API_KEY`。
普通 PR 任务跳过云评估，不接收 API Key。
仅显式云评估步骤接收此密钥，Pi 子进程不继承该密钥。
本地运行需安装相同的锁定测试依赖，并通过 shell 的安全环境设置密钥。
以下命令均需使用新的输出路径：

```sh
python scripts/smoke_owner_lifecycle.py --model-profile openrouter-cloud-smoke \
  --adapter-root /path/to/qdrant-memory-mcp --output /tmp/cloud-smoke-new.json
python scripts/evaluate_owner_lifecycle.py --model-profile openrouter-cloud-quality \
  --adapter-root /path/to/qdrant-memory-mcp --scenarios tests/quality/lifecycle-mutations-v1.json \
  --output /tmp/cloud-quality-new.json
```

### 免费路由和失败检查

运行器在推理前检查官方 Chat 或 Embedding 目录以及有效端点元数据。
它要求声明价格全部为零、支持文本输入及正确输出类型，且 Chat 支持 `response_format`。
随后通过实际严格 JSON 探针和实际向量批次检查能力。
元数据检查后，先运行向量探针，再运行 Chat。若 Chat 被拒绝，失败报告仍保留向量维度和模型 ID。
请求保留明确的固定 `:free` ID，要求支持 Chat 参数，并将 prompt 和 completion 价格上限设为零。
付费 ID 和 `openrouter/free` 均被拒绝。这些工作流不提供付费 opt-in。
[免费变体指南](https://openrouter.ai/docs/guides/routing/model-variants/free)说明了基础模型和变体的关系。
响应 ID 必须匹配选定的官方条目或目录绑定的 canonical 模型族。
仅当选定免费条目绑定该模型族时，才接受基础 ID。未知或混合模型族会失败。
报告保留请求 ID、实际 ID、允许的别名和元数据哈希。

云工作流配置共用一个账户并发组，不取消正在执行的运行。
全部请求串行执行，启动间隔至少为 3.1 秒。
每次请求超时 30 秒，最多尝试两次。
Cloud Smoke 总预算为 40 次 API 尝试，Cloud Quality 为 256 次，均包含元数据和重试。
临时网络错误、限流及指定服务端错误允许重试一次。
遵守有效的 `Retry-After`；超过有限等待预算时停止运行。
每日配额拒绝立即停止且不重试。其他认证和能力拒绝也会停止。
运行器不修改账户隐私设置，也不回退到其他模型。
免费路由可能退役、容量不足或用尽每日额度。这些情况产生 FAIL 及诊断分类。
基础设施失败与 `quality_gate` 分类不同，两者均不能产生 PASS。
对于已知的 HTTP 404 路由原因，区分参数不支持、账户数据策略限制和路由不可用。
报告仅保留固定分类和状态码，不保留供应商错误原文。

远程评估器仅允许使用固定匿名样例和 mutation 语料。
已知合成检索输入使用批量嵌入和临时的精确输入向量缓存。
无论成功或失败，均关闭缓存及客户端，并删除隔离的 Qdrant 数据。
远程模型权重、凭据和记忆数据均不进入 Actions 缓存。
远程配置复用依赖缓存，不下载 Ollama 安装包及权重。

### 查看结果和更换 Embedding 模型

从工作流运行的 Artifacts 区域下载 `owner-model-quality`。
JSON 报告记录源码、语料和配置指纹、模型 ID、真实向量维度、
API 尝试、HTTP 状态计数、有限重试、延迟百分位数和失败分类。
如供应商提供费用，则记录该费用。缺少费用数据视为未验证，不能记录为实测零费用。
质量报告保留 precision、正例召回率、Recall@5、MRR@5、mutation 和上下文结果。
冒烟报告保留每项生命周期检查，不能替代质量报告。

如需更换 Embedding 模型，将对应 Variable 或手动输入设为固定免费 ID。
探针在创建新测试数据之前探测替代模型的实际维度。
不同模型或维度不得复用已有 collection。
后续响应宽度变化会失败。将新运行保留为独立基线。
不要降低原有质量阈值以接受更换后的模型。

### main 上的首次云运行

位于 `f03d11d` 的[默认 Smoke 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39698763707)在元数据检查阶段失败。
Liquid Chat 免费端点状态为 `-5`。没有发送推理请求，客户端已关闭。
[默认 Quality 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39698820949)通过元数据检查，但 Gemma Chat 探针收到 HTTP 404。
[相同 Chat 的本地嵌入对照](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39699248654)也收到 HTTP 404。
这些运行没有执行基线评分，真实远程向量维度和模型质量仍未验证。
三份失败 artifact 均确认临时数据清理。对照运行也停止了测试专用 Ollama 进程。
初始诊断分类为 `model_unavailable`，未能识别具体 HTTP 404 路由原因。
这些失败不能证明语义质量低下。保留失败记录，在路由或能力修正后取得新证据。

所有质量配置使用完整的固定语料和相同质量阈值。
报告标识配置、生成路由、向量维度及本地模型摘要。
小模型结果不替代历史 reference 测量。
独立的双主体 Ollama 原生探针仅在 `reference` 配置运行。
每种质量配置均运行完整基线和 mutation 门禁。
仅有工作流配置不能证明已执行。应单独记录运行链接和实际结果。

每次手动触发仅运行一个选定配置，不运行模型矩阵。
使用工作流输入选择其他固定模型配置。模型名称属于配置，不需要 Secret。
本地模型变更必须提供固定的清单摘要。不接受任意未经校验的本地模型标签。
托管 Ollama 配置使用相同的嵌入模型、生成预算和关闭思考设置进行对比。
本地 1.7B 对比能以较低资源用量完成基线，但尚未通过质量门禁。
保留失败的质量结果。独立冒烟工作流不能提供质量 PASS。

[首次托管运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39570844284) 使用 `qwen3:4b-instruct-2507-q4_K_M`，结果失败。
Precision 为 1.0，但正例召回率和 Recall@5 均为 0.4375。Mutation 检查也失败。
Provider 错误和作用域泄漏均为零。这是失败证据，不能作为通过验收的证据。
[8B 托管尝试](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39572169059) 在基线完成前失败。
四次生成调用触及现有的 30 秒请求超时。此结果不能证明内存不足。
[3B 托管尝试](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39573848777) 也在基线完成前失败。
观测到五次模型响应，没有 provider 或记录错误。后台 ValueError 阻断了生命周期。
这不能作为模型质量通过的证据。实验性本地配置现使用更小的 `qwen3:1.7b`，保留相同的超时和质量门禁。
历史 Free Router 探索使用远程生成及本地 0.6B 嵌入模型。
该探索在基线完成前失败。当前固定基线拒绝动态路由，详见[托管 CI 证据](#2026-10-21-托管-ci-证据)。
质量阈值保持不变。
初始化将 Ollama 报告的已加载模型大小记录在 `quality-model-resources.json` 中。这不是进程内存峰值。
3B 尝试报告生成模型占 2,168,360,795 字节，嵌入模型占 1,237,875,425 字节，GPU 内存为零。
该次运行恢复了依赖和历史 Ollama 安装缓存。变化后的模型集合完成下载，并以新缓存键保存。
当前运行缓存已校验的安装包并重新解压，缓存的可执行文件不能绕过安装包验证。
1.7B 模型使用 [Apache License 2.0](https://ollama.com/library/qwen3:1.7b)；CI 仅下载进行评估，不在软件包中分发权重。

[首次 1.7B 尝试](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39578556397) 使用 provider 默认思考行为，在基线完成前失败。
观测到 27 次成功响应和 5 次 30 秒读取超时。模型调用 p50 为 10.74 秒，p95 为 30.03 秒。
Ollama 报告生成模型加载 1,886,629,395 字节，嵌入模型加载 1,237,875,425 字节，GPU 内存为零。
实验性 1.7B 配置现通过现有 Owner LLM 适配器显式请求 `reasoning_effort: "none"`。
Ollama 将其列为[关闭思考模式的 OpenAI 兼容控制](https://docs.ollama.com/api/openai-compatibility)。
报告记录此设置。30 秒超时、严格输出验证和质量阈值保持不变。
当所选 provider 支持时，Owner LLM 适配器接受可选值 `none`、`minimal`、`low`、`medium`、`high` 或 `xhigh`。
省略此选项时，请求保留 provider 默认行为。Reference 和 OpenRouter 配置省略此选项。

[关闭思考后的 1.7B 运行](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39580559444) 完成了基线，但质量门禁失败。
全部 29 次模型请求成功。模型调用 p50 为 1.14 秒，p95 为 4.37 秒。
Precision 为 1.0，但正例召回率和 Recall@5 均为 0.375。Mutation 检查也失败。
仅有更快的有效响应，不能使该模型成为合格的默认质量模型。
下一次托管对比使用 `qwen3.5:2b`。仅在 2B 候选未通过时测试 `qwen3.5:4b`。

[2B 对比](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39581374057) 在基线完成前失败。
模型成功加载并返回一次成功响应，随后后台 ValueError 阻断生命周期。
精确的验证失败原因尚未核实。该运行没有完整质量分数，也不能证明内存不足。
Ollama 报告生成模型加载 2,369,666,414 字节，嵌入模型加载 1,237,875,425 字节，GPU 内存为零。
此失败满足继续进行 4B 对比的条件，不放宽任何质量门禁。

[首次 4B 对比](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39582200266) 也在基线完成前失败。
观测到一次成功响应，随后后台 ValueError 阻断生命周期。
Ollama 报告生成模型加载 3,146,734,631 字节，GPU 内存为零。
两次 Qwen3.5 运行均没有完整质量分数。精确失败原因需要诊断复测。
诊断报告保留后台异常类型及源码位置，不保留异常消息或局部变量值。

[4B 诊断复测](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39583106933) 将失败定位到现有记忆重要性验证。
生成的重要性值不满足 0 到 1 的有限数值范围，Runtime 正确拒绝了它。
不能通过重新缩放无效输出或放宽验证获得 PASS。
保留关闭思考后的 `qwen3:1.7b` 作为可选对比模型。
它以低于已测 Qwen3.5 模型的加载内存与延迟完成了基线。
它仍未通过固定质量阈值，两个 Qwen3.5 候选均不构成合格替代。

### 容器替代方案

官方 [Ollama 容器](https://docs.ollama.com/docker) 可以隔离安装依赖。
它不会增加 RAM 或 CPU 资源、提高提取质量，或消除请求超时。
若添加容器测试，应固定镜像摘要，先启动服务再拉取模型，并在有截止时间的循环中轮询 API。
端口应仅绑定回环地址，挂载的模型目录应通过缓存跨运行保存，并在始终执行的步骤中删除测试容器。
仅使用 Docker volume 不能在全新的托管 runner 之间持久化数据。
不要用无限期的 `docker logs --follow` 命令作为就绪检查。
现有固定二进制方案已检查就绪、版本、模型摘要、清理和缓存恢复。
三次托管尝试均完成了初始化。已观察到的失败不足以支持增加第二套安装路径。

普通测试 CI 也运行评分器单元测试。

两个工作流均要求 Linux x86_64 和 Python 3.11。
它们安装 `requirements-quality.txt` 中带哈希的 wheel。
本地 Runtime 构建使用固定的构建后端，并离线检查依赖。
安装器保留正常依赖解析，并运行 `pip check`。
托管驱动 job 不加载模型，单独验证此环境。

更新快照时，从 Runtime 检出目录运行以下命令：

```sh
uv pip compile pyproject.toml requirements-quality.in --extra remote \
  --constraint requirements.txt --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --no-header --no-annotate --output-file requirements-quality.txt
```

使用新快照前，检查生成的版本与哈希。

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

reference 配置还测试同一轮输入替换两个不同项目事实的情况。
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

## 2026-10-21 托管 CI 证据

这些结果仅描述对应运行，不代表生产环境验收。

| 运行 | 结果 | 实测边界 |
|---|---|---|
| [Qwen3.5 0.8B 冒烟](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39613998288) | SMOKE_PASS | 八项功能检查全部通过。本次没有评估完整模型质量。 |
| [后续 Qwen3.5 0.8B 冒烟](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624059913) | FAIL | 两次请求均返回 HTTP 200，但 Pi 项目捕获未返回记忆。 |
| [固定 Nemotron 免费模型](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39617795232) | FAIL | 首次请求返回 HTTP 404，完整基线未执行。 |
| [固定 Dots 免费模型](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39618414645) | FAIL | 十次请求返回 HTTP 200，且免费模型 ID 与所选 ID 完全一致。下一次请求返回 HTTP 400，完整基线未完成。 |
| [Free Router 探索](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39619046518) | FAIL | 三次请求返回 HTTP 200，涉及两个模型。下一次请求返回 HTTP 404，完整基线未完成。 |
| [固定 Liquid 免费模型](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624008695) | PASS | 完整基线、mutation 检查及上下文检查通过。全部 29 次响应均匹配所选免费模型 ID。 |

Dots 运行选择了 `dots-studio/dots-3-note-preview:free`。
其 HTTP 400 响应的精确原因仍未验证。
后续报告通过 `failed_model_calls`，按冻结语料的案例 ID 标识失败的提取调用。
关系判断失败记为 `relation`；未知调用记为 `other` 或 `extract/unattributed`。
这些计数不包含原始输入、响应正文或异常消息。
Free Router 运行实际观察到 `liquid/lfm-2.5-2.6b:free` 和 `dots-studio/dots-3-note-preview:free`。
固定 Liquid 运行在 Runtime `0b1b05289eae3731af30e1c38c427aec7c6ac635` 上使用 `liquid/lfm-2.5-2.6b:free`。
它完成了 24 轮基线、32 个查询和 11 项 mutation 检查。
Precision 为 1.0，正例召回率、Recall@5 和 MRR 均为 0.9375。
作用域泄漏和 provider 失败均为零，重启未新增生成调用。
该记录说明一次运行通过，不保证端点始终可用，也不提供远程权重校验和。
每次已完成的外部运行均停止了测试自有的 Ollama 进程。
这些运行保留严格输出校验、TLS 检查、冻结语料和质量阈值。
它们使用免费路由，没有付费回退。

历史[本地 reference 证据](owner-model-stack-evidence.json)记录了 Qwen3 8B 与 Qwen3 Embedding 4B 的完整 PASS。
该结果仅适用于记录中的源码和模型 hash，不认证当前托管 CI 源码或免费路由。
模型目录声明支持某项能力，不保证端点对每次请求都可用。
功能冒烟验收与完整质量验收必须分开。
