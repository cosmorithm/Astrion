# Astrion Agent

> **以智慧，奔赴星海。**<br>
> *Intelligence bound for the stars.*

一个以科学为底色、以浪漫为想象、以乐观为行动力的 Agent 产品。

本 README 说明当前已经实现且可以运行的能力。项目总览与工作台启动方式见
[根目录 README](../README.md)，工具契约与状态设计见
[架构决策记录](../docs/adr/0001-tool-contract-and-task-state.md)。

当前版本保持“从零读懂 Agent”的原则：**ReAct 循环 + 可注入工具集 + 任务控制与证据工具 + 五层上下文管理 + 可恢复会话**。
基础 Agent 使用 Python 标准库；研究工作台与 BFCL 的可选依赖分别安装。

```
astrion-agent/
├── agent.py        循环、模型客户端、会话接入；保留原工具兼容导出
├── tools/          基础、资料、外部证据、计算工具与角色装配
├── research/       科研编排、存储、检索、外部服务、隔离执行与 API
├── tooling.py      ToolSpec / ToolCall / ToolResult 与调用验证
├── decision.py     共享的任务分解、调用复核与有界修正
├── decision_prompts.py  增强决策的版本化提示词与哈希
├── events.py       Run/Step/Event v1：结构化运行事实与 JSON 导出
├── memory.py       会话记忆：消息窗口 + 超额压缩 + 序列化
├── memory_evidence.py  工具观察转结构化证据的适配层
├── context_store.py    原始档案、scope 隔离与检查点
├── context_tools.py    证据搜索、重读与固定
├── context_adapter.py / context_service.py  可选服务端契约与参考服务
├── session.py      会话 JSON：原子保存、加载与恢复判断
├── benchmarks/     基准层：BFCL 官方基线 + Astrion 单轮决策适配
├── evaluation/     赛后评测：任务、运行器、可注册裁判
├── run_tests.sh / run_research_tests.sh    核心 / 科研扩展测试
├── show_bfcl_score.py  查看最近或指定 BFCL 运行成绩
└── sandbox/        基础 CLI 文件工具的应用层目录边界
    ├── data.txt    演示输入
    └── result.txt  运行产物
```

源码、BFCL 虚拟环境启动脚本和本地报告路径均已统一到 `astrion-agent/`，
仓库根目录不再保留旧目录名的兼容符号链接。历史运行仍可按运行 ID 查看或检查续跑。

工具控制协议泄漏到答案时，运行返回 error，不保存为研究报告；原始日志保留。修复依据与验证见 [P0事故记录](docs/incidents/2026-11-01-protocol-as-report.md)。

## 科研能力扩展

PDF/OCR、目录与接续阅读、多语言检索、联网证据、容器执行、产物交付、实际验收限制及模块关系见 [科研工作流](docs/RESEARCH_WORKFLOW.md)。

## 跑起来

需要一个 OpenAI 兼容的 API key，写在项目根目录的 `Astrion/.env` 里（见下）。

```bash
python3 agent.py "读取 data.txt，算出所有数字的和，写入 result.txt"
python3 agent.py                      # 不传任务就走默认任务
python3 agent.py "任务" --session session.json   # 可恢复会话
```

没有 key 时会直接报错退出，**没有离线兜底**——宁可失败，也不要给你一个假装在思考的假模型。

常用参数：

| 参数 | 作用 |
|---|---|
| `--steps N` | 本次最多进行的模型决策轮数，默认 10；恢复时表示还能再走几轮 |
| `--budget N` | 字符预算，默认 12000；持久化运行默认也将其数值用于独立的本地 token 预算，见上下文重构说明 |
| `--model M` | 临时换模型，覆盖 `.env` |
| `--base-url U` | 临时换服务端，覆盖 `.env` |
| `--session PATH` | 会话 JSON 路径；存在时恢复记忆，运行中每轮自动保存 |
| `--reply TEXT` | 回答 `needs_input` 会话的问题并继续原任务；需要 `--session` |
| `--decision-mode enhanced` | 启用增强决策；每个决策轮次使用 3 次模型调用，需修正时最多 4 次 |
| `--fresh` | 忽略已有会话文件，从空记忆开始（需配合 `--session`） |

## 配置

密钥放在上一级的 `.env`，不用每次 export：

```
Astrion/
├── .env              ← AGENT_API_KEY 写这里
└── astrion-agent/
```

`agent.py` 从自己目录向上找 `.env`（最多 3 层，越近越优先）。规则是**已有的环境变量不被覆盖**，所以 shell 里临时 `export` 的值总是赢。

优先级：`--model/--base-url` > 环境变量 > `.env`。

## 赛后评测

评测层把任务交给 agent，等运行结束后再让裁判查看运行记录。内置的 10 个任务在
`evaluation/cases.json`。例如 `sum_001` 提供 `data.txt` 的初始内容，要求 agent 写入求和结果，
由 `file_equals` 裁判检查 `result.txt`。

在 `astrion-agent/` 目录中，先查看任务清单（无需 API key，也不会调用模型）：

```bash
python3 -m evaluation --list
```

| 任务 ID | 评测内容 |
|---|---|
| `sum_001` | 正整数求和 |
| `sum_negative` | 负数、正数和零 |
| `sum_decimal_2dp` | 小数求和并按要求保留两位小数 |
| `sum_multiline` | 多行整数 |
| `sum_fullwidth` | 中文逗号分隔 |
| `sum_empty` | 空文件，约定和为零 |
| `copy_text` | 复制中英文文本 |
| `list_txt_files` | 按规则筛选和排序文件名 |
| `nested_sum` | 读取及写入子目录 |
| `sum_and_count` | 求和、计数，并由两个裁判分别检查文件 |

从 `astrion-agent/` 目录运行单个样例，或一次运行任务文件中的全部样例：

```bash
# 只运行一个任务
python3 -m evaluation --case sum_001

# 不传 --case：顺序运行默认 cases.json 的全部 10 个任务
python3 -m evaluation
```

这会使用与 `agent.py` 相同的 API 配置，调用真实模型。每个评测任务结束后会**自动保存一份 JSON 报告**到 `astrion-agent/eval_reports/`，
文件名包含任务 ID、UTC 时间和唯一标识；重复运行不会覆盖之前的报告。
全部任务结束后还会自动保存一个 `suite_` 开头的**总报告**，其中包含通过率、各任务结果和单任务报告路径。
运行日志、通过率和报告路径输出到 stderr，stdout 仍输出 JSON，包含 `reports` 和 `summary`。

不带 `--cases` 时，程序读取的是 `evaluation/cases.json` 中的任务列表，不会自动扫描目录里的其他 JSON 文件。

```bash
# 使用自定义报告目录
python3 -m evaluation --case sum_001 --report-dir /path/to/reports
```

报告会包含任务内容、初始文件、模型可见的消息和工具结果；评测器不会主动记录连接 API 的 key。
保存或分享报告时应留意这些内容可能包含任务数据。默认报告目录已加入 `.gitignore`，
自定义目录需要自行决定是否纳入版本管理。

agent 运行报错或评测环境准备失败时，也会尝试保存报告：后者的状态是 `setup_error`，
裁判标记为 `unknown` 且不会运行。任务文件不是有效 JSON、任务缺少 ID 等启动前配置错误
会直接报错，不会生成单任务报告。如果报告因目录或磁盘问题保存失败，且结果可序列化，stdout 仍给出运行结果，
其中 `report_path` 为 `null`、`report_error` 说明原因；CLI 同时在 stderr 提示并以非零状态退出。
如果结果本身不能转换为 JSON，CLI 会在 stderr 说明，无法提供 JSON 输出。
单任务报告或总报告保存失败都会使 CLI 以非零状态退出；裁判判定 `fail` 本身不会改变 CLI 退出码。
普通 `unittest` 测试结果仍显示在终端；其自动报告测试使用临时目录并在结束后清理。

其他常用参数：`--cases` 指定任务 JSON 文件，省略 `--case` 时顺序运行该文件的全部任务；
默认文件含 10 个任务，运行全部会产生多次真实模型调用。
`--steps`、`--budget`、`--model`、`--base-url` 分别覆盖 agent 的运行配置。

一次评测分为：

1. **准备任务**：为本次任务创建临时沙箱，只写入 `initial_files`。裁判参考答案保存在任务配置中，不随任务消息发给模型。
2. **运行 agent**：按 Event v1 记录模型调用、工具调用、压缩和结束状态；运行记录与可压缩的 `Memory` 分开。
3. **保存运行事实**：统计 `run()` 的耗时、已知 token 用量、最终文件快照和运行状态，然后清理临时沙箱。
4. **赛后评判和报告**：每个已注册裁判获取任务和运行记录的独立副本，返回自己的结论；每项任务的报告和整批的总报告分别写入文件。

总报告中的通过率按**任务样例**计算，而非按裁判个数计算：

- 一个样例的所有裁判都 `pass`，该样例才算通过；只要有裁判明确 `fail`，该样例就算失败。
- 没有裁判，或没有 `fail` 但存在 `unknown`、`pending`、`error`，该样例为待判定；裁判错误不会自动当作 agent 失败。
- `pass_rate_percent` = 通过数 ÷ 全部样例数；`scored_pass_rate_percent` = 通过数 ÷（通过数 + 失败数）。没有可判定样例时，后者为 `null`。
- 两个比例都只根据裁判结论计算。`run_status_counts` 和 `run_error_cases` 独立显示 agent 的结束状态和运行异常；例如最终文件正确但运行途中报错，也可能由裁判判为通过，运行异常仍会显示。

例如 10 个样例中通过 7 个、失败 2 个、待判定 1 个，总样例通过率是 70%，可判定样例通过率约为 77.78%。这些数字只是解释统计口径，不是实际评测结果。

运行时间使用 `time.perf_counter()`，从调用 `run()` 前开始，到其返回或抛出异常为止；
它包括模型等待、工具执行、记忆压缩和同步事件记录，**不包括**准备环境、生成文件快照或裁判评测。
每个裁判的 `duration_seconds` 单独记录。目前只记录时间，**没有任务时间上限，也没有时间通过阈值**。
原有的 agent `--steps` 步数限制和单次 HTTP 请求的 120 秒超时仍然存在。

每份单任务 JSON 报告包含 `case`（完整评测配置）、`run`（`result`、`duration_seconds`、
`model`、`agent_config`、`token_usage`、`run_id`、`event_schema_version`、`trace`、`artifacts` 和 `artifact_errors`）、
`judgments`（各裁判的结果和耗时）及 `report_path` / `report_error`（保存结果）。`run.result.status` 是运行状态；
`judgments` 中的 `pass` 或 `fail` 是裁判判断，两者独立。裁判错误是 `error`，
不是自动判定 agent 失败；证据不足可返回 `unknown`，等待人工处理可返回 `pending`。

`run.token_usage` 记录本次运行的已知增量，包括决策和摘要调用。`unknown_calls` 大于零、
或运行因异常中断时，`complete` 为 `False`；客户端没有用量统计时该字段为 `null`。
未来如果裁判自己调用模型，可以在裁判结果中附带自己的用量；第一版不会自动统计外部裁判的 token。

增加裁判时，可注册一个 `judge(case, run_record, spec) -> dict` 函数：

```python
from evaluation import default_registry, evaluate_case

registry = default_registry()

def observe_steps(case, run_record, spec):
    return {
        "status": "unknown",  # 这里只观察步数，不判定通过或失败
        "reason": "记录决策轮数",
        "evidence": {"steps": run_record["result"]["steps"]},
    }

registry.register("observe_steps", observe_steps)
# 把 {"name": "observe_steps"} 加入任务的 judges，再调用：
# report = evaluate_case(case, llm_factory, registry)
```

裁判结果的 `status` 可为 `pass`、`fail`、`unknown`、`pending` 或 `error`，还可附加
JSON 可序列化的 `reason`、`evidence`、`score` 等字段。裁判函数报错或未注册时，
该裁判记录为 `error`，其他裁判继续执行。CLI 当前只自动注册内置的 `file_equals`；
外部脚本、模型或人工裁判需要先封装并注册，人工复核工作流尚未实现。
`report["case"]` 和 `report["run"]` 也可以交给 `registry.evaluate(...)`，无需再次运行 agent。
如果通过 Python 接口得到多份单任务报告，可以调用 `summarize_suite(reports, report_dir=...)` 生成并保存总报告。

离线验证（使用模拟模型和 HTTP 响应，不访问网络）：

```bash
./run_tests.sh
# 或手动指定模块
python3 -B -m unittest -v test_session test_evaluation_summary test_evaluation test_run_lifecycle test_token_usage
```

## 输出怎么看

```
[1]
  think: 先看看目录里有什么。
  act:   list_files({"path": "."})
  obs:   {"path": ".", "entries": ["data.txt"]}
```

三段式就是 ReAct：

| 标签 | 是什么 | 代码里在哪 |
|---|---|---|
| `think` | 模型的自然语言推理 | `msg["content"]` |
| `act` | 结构化工具调用 | `msg["tool_calls"]` |
| `obs` | 工具的真实返回值，回填上下文 | `memory.add_tool(...)` |

`[2]` 的 think 引用了 `[1]` 的 obs——**这就是"循环"的全部含义**。

## 运行结果与结束方式

`run()` 返回字典，方便上层程序区分结束原因：

```python
{"status": "completed", "answer": "计算结果已写入文件", "steps": 4}
```

| `status` | 含义 | `answer` |
|---|---|---|
| `completed` | 模型发出了有效的 `finish` 调用 | `finish` 中的回答 |
| `no_tool_call` | 模型没有请求工具，主循环结束 | 模型文本，可能为空 |
| `step_limit` | 达到最大轮数，程序停止 | `None`，不能据此认定任务已完成 |
| `needs_input` | 模型调用 `ask_user`，等待用户补充 | 要向用户提出的问题 |
| `incomplete_plan` | 模型没有调用工具，但已声明的清单仍有待办 | 模型文本，不能认定任务已完成 |

`steps` 是主循环中已完成的模型决策调用次数，不包含用于记忆压缩的摘要调用。
程序也会在终端打印 `[状态]`。例如，模型调用 `finish` 并不证明目标文件一定正确，
它表达的是模型给出的完成信号。

每条工具调用都要有对应的结果。有效的 `finish` 会先写入一条 `tool` 消息，再结束。
如果同一批调用中还有排在 `finish` 或 `ask_user` 后面的工具，程序不执行它们，并写入“未执行”的结果。
非法 JSON、非对象参数、缺失或多余参数、类型错误，以及无效的 `finish.answer` 都会变成工具错误消息，交给下一轮模型；
如果已到达步数上限，则按 `step_limit` 结束。

多目标任务可以通过 `update_task_plan` 登记 `pending` / `completed` 子目标。清单有待办时，
`finish` 会返回错误；如果模型直接结束，状态是 `incomplete_plan`。清单由模型创建和更新，
无法保证模型列全目标或正确声明完成；真正的任务结果仍由外部裁判或用户验收。简单任务不强制建立清单。

工具契约由 `tooling.py` 提供：`ToolSpec` 是声明，`ToolCall` 是模型提议，`ToolResult`
记录验证或执行结果。`ToolRegistry` 在执行前检查工具名、JSON、必填项、类型和额外参数，
并在注册时检查支持的 schema 与 Python 函数签名。支持的 schema 子集为 `object`、`array`、
`string`、`integer`、`number`、`boolean`、`null`、`required`、`additionalProperties`、
`items`、`enum`、`minLength`、`minItems`；未支持的关键字会被拒绝，不声称实现完整 JSON Schema。
错误事件提供稳定 `code`，验证失败是 `rejected`，工具已开始后的异常是 `error`。
工具输出能力写入工具描述，系统提示要求只选能提供当前子目标所需结果的工具；这是一项模型引导，
不是已被证明的语义判断器，也不能保证模型不会错选工具。

`decision.py` 的增强模式把任务分解、参数来源、工具适用性、候选生成、参数检查和覆盖复核组合起来；
语义复核只记录疑问，不自动改写；只有确定性的工具调用结构错误才最多触发一次修正。
修正后若仍不合法、删光工具调用，或改动其他原本结构合法的调用，则保留初始候选。
真实 Agent 可通过以下命令使用同一决策模块：

```bash
python3 agent.py "你的任务" --decision-mode enhanced
```

默认仍是 `single` 模式。增强决策只产生候选行动；真实工具仍由 Agent 运行时的 registry
验证并执行。参数来源标签与必填/可选判断由模型提出；程序可检查引文是否出现在用户消息，
以及工具 schema 是否声明必填和匹配的默认值，但引文存在不证明语义或参数值正确。增强决策的规划
与复核可能遗漏或误判，不等于独立裁判。

增强提示词位于 `decision_prompts.py`，当前版本 `astrion-decision-v2.0`，规划和复核输出
使用 schema version 2：`quote` 只放用户原文，`explanation` 单独放模型解释；schema 中
确有的缺失参数与 `capability_gaps` 分开。复核意见采用类型、描述、引用、工具与调用索引；
引用、名称和索引的核对只说明引用是否存在，不验证语义。模型输出不符合协议时会记录
`plan_error` / `review_error` 并使用空规划或空复核，不因此触发语义修正。
提示词版本和各静态模板的 SHA-256 写入 `decision.finished` 和 BFCL 报告；模板哈希不代表
完整请求或用户上下文的哈希。历史报告仍按原格式读取，不将旧版分数冒充新版结果。

离线验证主循环与 token 计量（模拟模型和 HTTP 响应，不访问网络）：

```bash
python3 -B -m unittest -v test_run_lifecycle test_token_usage
```

## 结构化运行事件（events.py）

`Message` 保存模型需要看到的上下文；`Event` 记录运行时真实发生的事情；文件等任务产物由
评测报告的 `artifacts` 保存。三者不再混成一份终端文本。

每条 Event v1 都有相同外壳：

```json
{
  "schema_version": 1,
  "event_id": "run_abc:event:000007",
  "run_id": "run_abc",
  "sequence": 7,
  "type": "tool.call.finished",
  "occurred_at": "2026-10-30T12:00:00.123Z",
  "step": 2,
  "payload": {}
}
```

`sequence` 是一次运行内的权威顺序；时间戳用于观察，不用于解决事件先后冲突。当前事件类型覆盖：

- `run.started` / `run.finished`；
- `model.call.started` / `finished` / `failed`；
- `decision.finished`（增强模式的计划、来源核对、复核与修正记录）；
- `tool.call.started` / `finished`；
- `memory.compaction.started` / `finished` / `failed`。

工具终止事件用 `outcome` 区分 `success`、`error`、`rejected` 和 `skipped`。只有已经通过
JSON 解析、工具查找和参数绑定的调用才会产生 `tool.call.started`；因此 started 后没有
finished，表示工具执行或事件记录在中途被打断，需要按“不确定副作用”处理。

第一版是**旁观日志**：Event 不参与 session 恢复，也不能重放任务。模型或进程异常时会留下
`model.call.failed` 或最后一条已成功记录的事件，但不会伪造 `run.finished`。事件 sink 在运行中失败会
立即中止 Agent；运行结束后单独导出 JSON 失败，不会倒改已经得到的任务结果。

程序化记录和导出：

```python
from events import EventRecorder

recorder = EventRecorder()
result = run(task, llm, memory, event_sink=recorder)
recorder.save("events.json")
```

模型调用事件只保存输入消息数量、字符数等元数据，不在每一轮重复复制完整 prompt；完成事件保存
assistant 响应、单次 usage 和耗时。工具结果目前原样保存在事件中，所以事件文件可能包含用户数据，
POSIX 下导出文件默认权限为 600，分享前仍需检查内容。

评测报告继续使用 `run.trace` 承载这组 Event v1，并另外保存 `run_id` 和
`event_schema_version`。这样裁判、调试和未来恢复层消费同一份运行事实，不维护第二套平行日志。

离线验证：

```bash
python3 -B -m unittest -v test_events test_run_lifecycle test_evaluation
```

## Token 计量

每次模型调用结束后，读取 API 响应中的 `usage`，显示本次和累计用量。例如：

```text
  [token][决策] 本次：输入 800 / 输出 50 / 合计 850
  [token] 累计：输入 800 / 输出 50 / 合计 850
  [token][记忆摘要] 本次：输入 300 / 输出 40 / 合计 340
  [token] 累计：输入 1100 / 输出 90 / 合计 1190
```

上面的数字仅用于演示。三个数分别来自服务端的 `prompt_tokens`、
`completion_tokens`、`total_tokens`，不根据字符数推算。

- **在 `ChatLLM._post()` 统一记录。** 普通决策和记忆摘要都计入，因此压缩记忆的调用不会漏算。
- **未知用量不当作零。** 如果 `usage` 缺失或三个字段不完整、无效，本次显示“未知”；
  已知累计会注明还有多少次未知调用未计入，全部未知时累计也显示“未知”。
- **累计范围是一个 `ChatLLM` 实例。** 命令行每次启动创建一个新实例，从零累计；token 用量没有持久化到磁盘，也不写进会话文件。
- **计量与预算分开。** `--budget` 的字符数与模型 token 使用量不同；持久化运行还会设置独立的本地 token 预算，SS 与 legacy 的触发规则见下文。
  此次只记录返回的实际 token 用量，不增加 token 上限或费用换算。

实现入口是 `ChatLLM._record_usage()`：`usage_totals` 保存已知累计，
`usage_calls` 记录收到响应的调用次数，`usage_unknown` 记录用量未知的次数。

离线验证（模拟 API 响应，不读取密钥或访问网络）：

```bash
python3 -B -m unittest -v test_token_usage
```

## 代码地图

| 层 | 位置 | 说明 |
|---|---|---|
| 工具契约 | `tooling.py`，`agent.py` 的 `TOOL_SPECS` | 工具声明、调用、结果、注册与验证；生成模型工具清单 |
| 工具实现 | `list_files` 等 | 真正执行的函数 |
| 策略层 | `safe_path()` | 路径钉死在 sandbox，越界即抛错 |
| 模型层 | `ChatLLM` | 一个 `_post` 管所有 HTTP，`chat` 支持可选 tools，`summarize` 共用 |
| 编排层 | `run()` | 那个循环，ReAct 的核心都在这里 |
| 运行事实 | `events.py` | Event v1 外壳、连续顺序校验、内存记录与原子 JSON 导出 |
| 记忆层 | `memory.py` 的 `Memory` | 消息窗口 + 压缩 + 序列化 |
| 持久化 | `session.py` | 会话 JSON 的原子保存、加载与恢复判断 |
| 基准层 | `benchmarks/` | 通用原始响应适配 + BFCL 官方旁路；默认 agent 不依赖 |
| 终止 | `finish` 工具 + `max_steps` | 显式终止 + 预算硬停 |

## 五层上下文管理

上下文现在分为三部分：**原始档案、常驻任务与证据、当前模型视图**。五层修改的是模型视图；原始消息和工具观察保存在独立档案，不随压缩删除。这里是 Astrion 的工程实现，借鉴分层范式，不声称复刻特定版本 Claude Code 的内部实现。

| 层 | 触发与实现 | 是否调用摘要模型 |
|---|---|---|
| 1. Tool Result Budget | 单个工具结果超过 3,000 字符时外置，保留前 240 字符预览及内容引用。受限分页的 `read_evidence` 返回不立即再次外置，保证重读可用。 | 否 |
| 2. History Snip | 只裁相邻、参数与结果相同、无助手说明的成功只读批次；写操作、失败结果和用户要求不按此规则裁剪。 | 否 |
| 3. Microcompact | 仍超预算时，将最近 4 个非用户单元之前、超过 500 字符的成功结果正文替换成可重读凭据，保留调用与结果配对。 | 默认否；可选 HTTP 服务只选择候选 ID |
| 4. Context Collapse | 仍超预算时，最多折叠 4 个旧非用户单元，生成局部进度/待办摘要并记录原始片段引用；近期单元保留。这里的记录批次结束不等于科研任务完成。 | 有合适旧单元时调用 |
| 5. Auto-compact | 压力仍在时重建工作摘要，保留所有用户原话、固定证据、任务清单和至少最近 1 个完整单元。 | 有可压缩旧内容时调用 |

管线按顺序执行，未满足触发条件的层记录为跳过。前三层可避免后续摘要调用；一次管线最多调用两次摘要模型。第 4 层返回空白、结构错误或证据编号不匹配时，本次不立即在第 5 层重复请求摘要。

### 信息保留与校验

- 用户原始任务及后续补充保持 `user` 原话。任务清单来自运行状态，压缩后仍注入。
- 原始消息及所有工具观察写入档案；文档、文件、计算、子 Agent 报告还有结构化证据适配，保留实际观察到的文本和来源。未知工具也保留原始返回，但不会自动推断其事实结构。
- 关键内容由调用方或模型通过 `pin_evidence` 显式选择。程序要求固定内容是来源中的连续原文，单项最多 1,000 字符，固定项序列化总量最多 2,000 字符。选择不全面仍可能影响回答，程序不假定模型会选对所有重点。
- 摘要必须包含非空 `progress`、`pending`，及与固定项一致、无重复的 `evidence_ids`。生成文本不替代固定原文。系统校验结构、编号与消息配对，不判断摘要的全部语义是否正确。
- 每层先构造候选，再确认比当前视图更短后提交。后续层拒绝摘要，不撤销前面已验证的缩减；原始档案始终保留。摘要服务异常沿现有错误路径上报。
- 摘要、引用和来源是历史数据，以 `assistant` 消息提供；不会升级成系统规则。子 Agent 返回仍是未经独立验真的报告。

默认 `budget_chars=12000` 统计序列化消息字符数，**不含工具 schema，不是 token 硬上限**。固定要求或近期内容过大时，`over_budget=true` 如实报告；不会静默删除用户要求来声称达标。摘要模型调用照常计入实际 token 统计。

### 物理存储与恢复

项目资料、全局对话记忆和项目记忆仍在 `research_data/astrion.sqlite3`。新增的 `research_data/context.sqlite3` 保存原始消息、工具观察和上下文检查点；研究模式跟随所配置资料数据库的目录，基础 CLI 可通过 `ASTRION_CONTEXT_DB` 指定路径。

同一文件中每个主/子 Agent 有独立随机 `scope`，读取引用只能访问当前 scope。引用按内容及元数据的 SHA-256 生成，内容变化形成新观察；恢复时校验哈希。原始日志只追加，工具批次完整结束后保存工作视图检查点。进程中途失败时恢复上个完整检查点，未完成尾部仍留在原始日志中；这不提供工具执行的 exactly-once 保证。

研究运行结果及子 Agent 条目包含 `context_archive.path` 与 `scope`。代码中可用 `Memory.from_archive(path, scope)` 恢复记忆，`memory.runtime` 提供 task/status/steps/task_plan 等元数据；这不是前端的一键续跑入口。基础 CLI 的 `--session` 仍保存可搬运的 JSON 快照，包含档案内容。旧会话仍可加载，旧版本已经丢失的原文不能凭空恢复。

直接构造 `Memory(...)` 默认使用内存档案，适合库调用和离线测试；CLI 与研究入口明确接入持久化存储。档案目录被 Git 忽略，当前没有自动清理策略。保存的是工具实际返回的观察，不能恢复工具自身截断前、从未返回过的内容。

### 搜索、重读与固定证据

所有运行中的 Agent 都获得以下受限工具，引用和 Step ID 都限定在当前 Agent 作用域：

```text
search_evidence(query, offset=0)        # 每页 5 条；查找旧引用与片段
read_evidence(ref, offset=0, limit=2000) # 字符分页，最大 4000；返回来源与 next_offset
pin_evidence(ref, quote)               # 固定原文；quote="" 显式取消固定
search_step_history(query="", offset=0) # 查询本 Agent 的 Step 目录，每页 5 条
read_step_history(step_id, level="RAW", offset=0, limit=2000) # 重读 Step 或已生成摘要
```

原始工具结果在发送给模型时附带 `_context.result_ref` 与 `_context.evidence_refs`。大结果的凭据同样携带引用。取消固定不删除原文，重读的是当时观察到的快照；需要最新外部状态时应重新调用原工具。

### 按 Agent 分隔的 Step 历史与上下文重构

**默认使用五层压缩（`legacy`）**：普通 Agent、研究主 Agent 和子 Agent 均走 `Memory.compact`，不会自动生成 SS 或调用 SS Planner。Step 历史、快照和证据仍按 Agent 保存；续写已有会话时保留旧档案与已有摘要，之后的压缩使用五层策略。下文的 SS 方案仅在 Python 接口显式设置 `context_strategy="ss"` 或对照实验指定 SS 时启用；研究运行会把显式选择的策略传给子 Agent。五层压缩可能无法把每个请求压到本地 token 硬上限以内，发送前检查仍会阻止超限请求。

CLI 和研究入口为每个 Agent 的 `scope` 在 `context.sqlite3` 中记录 Step、执行事件与每次模型请求的消息/工具快照。主 Agent 与每个子 Agent 的历史相互隔离；子 Agent 完成后记录仍保留，主 Agent 只能看到显式返回的结果。继续研究时，主 Agent 的已有记录被复制到新的 scope，原记录不变；子 Agent 私有记录不会自动并入。正在执行中断时留下的 Step 可以处于 `running`，不能据此宣称工具已经执行完毕；旧运行没有的新字段也不会被追溯补造。通过 `Memory(...)` 直接构造的内存档案不会持久化，调用方需提供文件型 `ContextStore`。

启用本地 token 预算时，SS 策略在使用量达到本地预算约 80%、且已有至少一个已记录 Step 时，尝试从该 Agent 的完整档案重新构建上下文；此时不再同时用字符水位触发，以免重构后立即再次重构。没有 token 预算时沿用字符水位，legacy 策略保持原有触发规则。系统规则、用户消息、固定证据、任务状态和模型尚未处理的最新工具调用及结果仍保留；无法归属已记录 Step 的旧版折叠内容为兼容而保留。其他已完成 Step，包括最近的历史，都通过 SS 目录交给 Planner 选择，不再固定保留最近四步。若只有最新工具批次、没有其他待选 Step，则无需调用摘要模型。

各 Step 的 SS 仅在重构时逐条生成，使用本地 tokenizer 校验正文不得超过 80 token；超长或格式不符最多重试一次，仍失败则保存明确标记的降级条目。原始事件可搜索和重读，摘要按来源版本、档位及 tokenizer 指纹隔离缓存。`0.5B` 是软目标：优先保留完整最新反馈，依照 Planner 优先级在目标内加入历史并尝试较短档位；选中的 SS 可以扩展目标，但不能超过 B。增长空间因此可能减少，并不保证下次重构前能运行多个 Step。若装不下必要内容或首条选中的历史摘要，最新工具结果可以变成标明截断、可通过 `read_evidence` 分页重读的预览；为历史腾空间时尽量保留至少 240 字符反馈，无法同时满足则省略该历史。工具调用配对与档案原文保留。不可缩减内容超过 B 时停止发送。

完整目录放不下规划请求时，程序按历史顺序建立阶段目录、选择阶段并展开能放入预算的 Step；未展开记录仍在当前 Agent 档案中。Planner 选择按优先级由程序组装：超限时依次尝试已缓存的较短档位和 SS（不会为退档额外调用模型），仍放不下就省略并记录原因。所有工具调用配对、受保护消息和本地 token 限制都由程序复核；无法构造合规视图时保留旧视图。`search_step_history` 可搜索摘要及本 Agent 的原始事件，`read_step_history` 可分页读取原始记录。摘要可能遗漏或失真，不能替代证据。

预算恢复的回执、失败诊断与复现命令见 [上下文预算恢复](docs/CONTEXT_BUDGET_RECOVERY.md)。 长科研任务中的旧压缩与 SS 对照见 [长程科研 Context 评测](evaluation/long_research/README.md)。

**本地计数边界：** `B` 为输入预算，CLI/研究模式默认采用字符预算的数值作为独立的本地 token 预算，也可在根 `.env` 设置正整数 `ASTRION_CONTEXT_INPUT_BUDGET_TOKENS`。程序将消息、工具 schema 按固定 JSON 格式序列化，用本地 tokenizer 精确计数；`0.5B` 是软目标，当前实现可为保留反馈及选中的短摘要扩展至 `B`，每次模型请求的本地计数始终不得超过 `B`。模型输出及服务端封装需另外预留空间。直接使用内存型 `Memory` 的库调用，如需对每次请求启用预算，也应显式设置该环境变量。

本地 JSON 不是供应商服务端的隐藏聊天模板，**本地严格上限不等于服务端实际 token 的数学保证**；服务端 usage 才是实际处理的计量。DeepSeek 官方同样指出不同模型分词不同、应参考返回的 usage：[Token & Token Usage](https://api-docs.deepseek.com/quick_start/token_usage/)。如需服务端绝对上限，需供应商提供与实际请求一致的权威预检能力；目前不能凭估算声称已保证。无法加载配置的 tokenizer 时，持久化模型运行会失败并给出错误，不会偷偷改用估算值。

首次使用当前官方 DeepSeek tokenizer（模型更新后须重新核实匹配性）：

```bash
pip install -r requirements-context.txt
python -m context_tokenizer install-deepseek
```

安装器从 DeepSeek 官方下载 tokenizer，校验固定 SHA-256，不覆盖已有不同文件；默认保存到被 Git 忽略的 `research_data/tokenizers/deepseek-v4/tokenizer.json`。其他模型在 `.env` 设置 `ASTRION_CONTEXT_TOKENIZER_PATH` 指向匹配的 Hugging Face `tokenizer.json`，同时根据模型和服务端开销配置预算。tokenizer 的内容哈希进入摘要契约，换 tokenizer 后不会直接重用旧契约摘要。单个 Step 拆分超过 32 片时，构建会失败而不丢原文。新增模型调用会增加用量，需通过实际任务评测质量与成本。

研究 API 通过项目和运行记录验证后，提供 `GET /api/projects/{project_id}/runs/{run_id}/agents` 列出本次 Agent，以及 `GET /api/projects/{project_id}/runs/{run_id}/agents/{agent_id}/steps?offset=0&limit=20` 列 Step；加 `step_id` 参数可分页查看该 Step 事件和当时模型请求快照。`agent_id` 为 `root` 或对应 `worker_1` 等。快照仅保存程序实际收到或发送的内容，不代表供应商内部未返回的思考；它可能含用户资料和模型输入，应按研究数据保护。事件中的完整快照也可能较大，注意保存周期和访问权限。上下文构建记录位于同一个数据库的 `agent_context_builds`，每次请求快照位于 `agent_context_snapshots`。

验证新增 Step 历史与重构测试：

```bash
python -m unittest discover -s tests/context -q
```

### 上下文对照实验

固定历史的追加、旧压缩和 SS 重构对照见 [Context Eval v1](evaluation/context/README.md)。实验保存每次模型调用、结果与费用信息；它是合成历史重放，不代表完整科研 Agent 的任务表现。

### 可选服务端适配

默认五层均在客户端组织，不绑定模型供应商。第 3 层支持 `HttpContextAdapter`：在根 `.env` 配置 `ASTRION_CONTEXT_SERVICE_URL`，以及可选的独立 `ASTRION_CONTEXT_SERVICE_KEY`、`ASTRION_CONTEXT_SERVICE_TIMEOUT`（默认 5 秒，最大 30 秒）。密钥不会复用模型 API key 或保存进会话。

服务端收到以下 HTTP v1 契约，**不接收原始证据或用户消息**：

```json
{"version":1,"operation":"select_tool_results","budget_chars":12000,"candidates":[{"id":3,"tool":"read_file","chars":2000}]}
```

返回格式：

```json
{"version":1,"clear_ids":[3]}
```

返回 ID 必须属于客户端预先允许的候选，不能重复。超时、连接失败、错误响应、越界 ID 都回退客户端规则，并记录原因。此接口是独立的上下文编辑服务，**不是原生 Claude 缓存编辑，也不会把供应商私有字段塞进 Chat Completions 请求**。原生供应商能力仍需对应协议适配。

提供可直接运行的确定性参考服务，选择全部已允许候选，不调用模型：

```bash
python3 -m context_service --port 8766
# 随后在项目根 .env 中设置：
# ASTRION_CONTEXT_SERVICE_URL=http://127.0.0.1:8766/context-edits
```

参考服务自身不读取 `.env`；如果配置服务密钥，应以相同的环境变量启动服务。服务默认只监听本机回环地址。可以替换远端实现或在 Python 中注入具有 `select(candidates, budget_chars)` 方法的适配器，客户端校验继续生效。

### 观测与验证

`memory.compaction.finished` 包含五层 `layers`，逐项记录名称、是否改变视图、前后字符数、原因、相关 ID 或归档引用，以及服务端回退信息。还有 `archive_scope`、固定证据数量和 `over_budget`。`memory.compaction.failed` 保留已执行层的报告。摘要调用在既有 `model.call.*` 事件中计量。

```bash
python3 -B -m unittest test_memory test_context_pipeline test_session test_research
bash run_tests.sh
.venv-ui/bin/python -B -m unittest test_research_api test_research_http
```

回归覆盖逐层行为、重读不会立即再次外置、原文分页与固定、跨 Agent 隔离、原始日志不变、数据库损坏、重启恢复、服务端越界与回退，以及真实本机 HTTP 协议。它验证的是工程行为，不等同于真实模型研究质量或 BFCL 得分提升。

## 会话持久化（session.py）

`--session` 把记忆、任务、子任务清单、结束状态和累计步数写进一个 JSON 文件；默认不开启。

```bash
python3 agent.py "读取 data.txt..." --session session.json
# 中断、崩溃或达到 --steps 后，重跑同一条命令即可继续
python3 agent.py "读取 data.txt..." --session session.json

# 忽略旧文件，从空记忆开始；第一次保存时覆盖
python3 agent.py "读取 data.txt..." --session session.json --fresh

# 如果 Agent 通过 ask_user 请求补充信息，回答问题并继续原任务
python3 agent.py --session session.json --reply "文件名是 data.txt"
```

规则：

- **保存点在每轮结束。** 只有一整批工具结果都写回 `Memory` 之后才保存，文件里的历史始终能通过 `check()`。如果进程在模型请求或一批工具执行中途终止，恢复点最多回退到上一轮，那一批工具可能重做。
- **原子写入。** 先写临时文件并 `fsync`，再用 `os.replace` 替换；不会留下半个 JSON。POSIX 下会话文件权限设为 600。
- **按任务判断是否继续。** 同一任务且上次状态是 `running`、`step_limit` 或 `incomplete_plan` 时，自动继续；`needs_input` 需要通过 `--reply` 提供补充信息。原任务不重复加入记忆。
- **旧会话兼容。** 没有 `task_plan` 字段的 v1 会话按空清单加载；恢复后保存时会增加该字段。
- **恢复时的配置来自当前命令行。** 系统提示、`--budget` 和 `--steps` 使用本次运行的值；`--steps` 表示本次还能进行多少轮决策，结果里的 `steps` 是会话累计值。
- **内容可能敏感。** 会话文件包含模型可见的消息和工具输出，可能含文件内容；默认已被 `.gitignore` 的 `session*.json` 覆盖，不要提交或分享。
- 会话文件损坏、版本不兼容或历史配对不自洽时，CLI 会报错退出；可用 `--fresh` 忽略旧文件重新开始。
- 会话写入失败会中止本次运行；上一次成功保存的文件保持不变，错误会输出到 stderr。

## 基准测试层（benchmarks/）

这一层不进入默认 `agent.run()`。通用原始响应适配仍然存在，同时已经正式接入
BFCL V4 官方 harness；ToolBench 暂不接入。

- `ChatLLM.chat(messages, tools=None)`：不传 `tools` 时仍发送默认 `TOOLS`；传列表时按 case 注入；传 `[]` 时省略 `tools` 和 `tool_choice`。
- `FunctionCallCase`：保存 `id`、`category`、`messages`、`tools` 和 `metadata`。
- `run_cases()`：逐条调用模型，记录原始请求/响应和错误；不判断对错。
- `benchmarks/bfcl.py`：编排官方模型基线和 Astrion 单轮路径，统一调用官方 scorer。
- `benchmarks/bfcl_official.py`：不改第三方安装包，在当次子进程内覆盖官方 DeepSeek handler 的实际 API 模型名。
- `benchmarks/bfcl_astrion.py`：把官方单轮 case 交给 `ChatLLM`，保存 Event v1，并转换为官方结果格式；不执行 case 工具。
- `benchmarks/manifests/bfcl_v4_single_turn_baseline_200.json`：默认基准，5 类各 40 条，共 200 条。
- `benchmarks/manifests/bfcl_v4_all_scoring_full.json`：官方 `all_scoring`，22 类、5,217 个生成条目、预计 5,106 个计分目标。
- `benchmarks/manifests/bfcl_v4_single_turn_smoke.json`：保留原来的 20 条 smoke 子集。
- `benchmarks/manifests/bfcl_v4_single_turn_trial_3.json`：正式调用前后的最小 3 样例试跑。
- `setup_bfcl.sh`：在 `.venv-bfcl/` 安装可选依赖，不污染零依赖核心。

快速查看将要执行的正式命令，不安装 BFCL、也不调用模型：

```bash
python3 -m benchmarks bfcl run --dry-run
```

安装后，在项目根目录已有的 `.env` 中增加 BFCL 的专用密钥：

```bash
./setup_bfcl.sh
# Astrion/.env
DEEPSEEK_API_KEY=你的_DeepSeek_官方_key
python3 -m benchmarks bfcl doctor
```

BFCL 命令会自动查找 `.env`，且不会覆盖 shell 中已经 `export` 的同名变量。
`.env` 已被 Git 忽略；不要把真实密钥写进 README、报告或提交记录。

运行默认 200 条基准子集：

```bash
python3 -m benchmarks bfcl run
```

为了与 Astrion 的现有 200 条运行做同模型对照，这条命令现在保持 BFCL
registry/scorer 为 `DeepSeek-V3.2-Exp-FC`，但官方 `DeepSeekAPIHandler` 实际发给
DeepSeek API 的 `model=` 默认覆盖为 `deepseek-flash`。报告会分开记录
`registry_model` 和 `api_model`；默认温度也与 Astrion 对齐为 `0`。如需复现旧的
官方映射和原温度，显式使用：

```bash
python3 -m benchmarks bfcl run \
  --api-model deepseek-chat \
  --temperature 0.001
```

覆盖后的 flash baseline 是项目内严格对照，不冒充 BFCL 官方默认配置或排行榜分数。

### 固定对照柱：BFCL 200（2026-10-31）

下面这次运行是 Astrion 后续优化使用的**固定 baseline**，不是“自动取最新成绩”。
除非基准版本、样例集合或模型发生变化，否则后续实验都应保留它，并把新结果追加为
新的实验记录，不能覆盖或改写这组数字。

| 项目 | 固定值 |
|---|---|
| 运行 ID | `20261030T164520_712818Z_DeepSeek-V3.2-Exp-FC_api-deepseek-flash_b022dfbb` |
| Astrion 代码提交 | `c1926390c3bdb7e4201b1b27ff69a3537c4fa595` |
| BFCL harness | `bfcl-eval==2026.3.23`，BFCL V4 |
| manifest | `bfcl_v4_single_turn_baseline_200` |
| manifest SHA-256 | `5987a1e2db7ab790825ff336be29ce3d88c898b7e679fc889edc3e8c4edbbbf6` |
| 样例 | 200 条；`simple_python`、`multiple`、`parallel`、`parallel_multiple`、`irrelevance` 各 40 条 |
| 生成链路 | BFCL 官方 `DeepSeekAPIHandler` + 项目内 API model 覆盖器 |
| registry / scorer | `DeepSeek-V3.2-Exp-FC` |
| 实际 API model | `deepseek-flash` |
| temperature / threads | `0` / `1` |

固定 baseline 成绩：

| 类别 | 正确数 | 准确率 |
|---|---:|---:|
| `simple_python` | 36/40 | 90.0% |
| `multiple` | 38/40 | 95.0% |
| `parallel` | 38/40 | 95.0% |
| `parallel_multiple` | 32/40 | 80.0% |
| `irrelevance` | 32/40 | 80.0% |
| **总计** | **176/200** | **88.0%** |

| 效率指标 | 固定 baseline |
|---|---:|
| 输入 token | 104,521 |
| 输出 token | 49,812 |
| 总 token | 154,333 |
| 模型延迟合计 | 354.38 秒 |
| 生成命令墙钟时间 | 360.78 秒 |

同一批 case、同一 API model 和同一 scorer 下，当前 Astrion 优化起点是运行
`20261030T161221_406989Z_Astrion-deepseek-flash_2a70f301`（提交 `f46ef0f`）：

| 路径 | 总分 | simple | multiple | parallel | parallel_multiple | irrelevance | 输出 token | 模型延迟 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 固定 baseline | 176/200 | 36 | 38 | 38 | 32 | 32 | 49,812 | 354.38 秒 |
| Astrion 起点 | 175/200 | 36 | 38 | 37 | 33 | 31 | 63,005 | 462.53 秒 |

两组的 200 条输入 token 逐题相同。配对结果是 169 题共同正确、18 题共同错误、
baseline 单独正确 7 题、Astrion 单独正确 6 题；当前 0.5 个百分点的差异不足以说明
任一路径准确率更强。这个 baseline 用于工程回归和项目内部对照，不是 BFCL 官方
full leaderboard 分数。

运行官方全部正式计分类（高成本，建议先 dry-run）：

```bash
python3 -m benchmarks bfcl run --benchmark all-scoring --dry-run
python3 -m benchmarks bfcl run --benchmark all-scoring
```

`all-scoring` 会处理 5,217 个生成条目，其中 memory 的 111 个前置会话不单独计分，
预计最终计分目标为 5,106 个。它包含 live、多轮、memory 和 web search，调用次数、
时间和费用都远高于 200 条默认基准；项目不会自动运行它。

运行时终端会显示当前/最近是第几组、本组完成数、总条目进度和评分组数；类别、
完成组或总进度跨过约 1% 时输出，无变化时每 60 秒打印心跳。状态也会写入本次目录的
`bfcl_progress.json`。默认每 5 秒检查一次，可用 `--progress-interval 10` 调整，
不会增加模型调用。

中断后先做只读审计，再确认是否从原目录续跑：

```bash
# 不改文件、不读密钥、不调用模型
python3 -m benchmarks bfcl resume --run <运行ID> --check

# 备份并修复中断状态，然后调用官方 generate/evaluate 继续
python3 -m benchmarks bfcl resume --run <运行ID>
```

续跑不会使用 `--allow-overwrite`。成功结果原样保留；官方写入的推理错误会被移出
工作结果并重试。memory 前置会话按“结果 + checkpoint”的连续有效前缀恢复，修复前
文件保存在本次运行目录的 `resume_backups/attempt_NNN/`，每次尝试的日志和状态写入
`attempts` 历史。`--check` 只是计划，只有第二条命令才可能产生 API 费用。

若要重跑原来的 20 条 smoke：

```bash
python3 -m benchmarks bfcl run \
  --benchmark smoke-20
```

若只想先验证 3 条：

```bash
python3 -m benchmarks bfcl run \
  --benchmark trial-3
```

上面测试的是 BFCL 官方 model handler 基线。若要用同一批官方 case 和 scorer 测试
Astrion 的单轮工具选择链路，先 dry-run，再显式运行：

```bash
python3 -m benchmarks bfcl astrion-run --benchmark trial-3 --dry-run
python3 -m benchmarks bfcl astrion-run --benchmark trial-3
```

Astrion 路径会调用 `ChatLLM.chat(messages, tools)`、记录每个 case 的 Event v1，并把
建议的函数名和参数交给官方 BFCL scorer；它不会实际执行 benchmark 工具，也不进入
多步 ReAct 循环。当前只支持 `trial-3`、`smoke-20` 和 `baseline-200` 三个固定单轮
子集，不接受含多轮和 memory 类别的 `all-scoring`。报告中的 `execution_path` 会将
Astrion 结果与官方 handler 基线区分开。

使用与真实 Agent 相同的增强决策模块测试 BFCL 200：

```bash
python3 -m benchmarks bfcl astrion-run --benchmark baseline-200 \
  --api-model deepseek-flash --mode enhanced
```

先验证 3 条可将 `baseline-200` 改为 `trial-3`；添加 `--dry-run` 只检查命令，
不读取密钥、不调用模型。增强模式每题正常使用 3 次模型请求，需修正时使用 4 次；
200 条约为 600–800 次模型请求，费用和耗时高于单次路径。BFCL 工具不会执行，也不伪造结果。
运行报告记录每个阶段的调用、用量、初始和最终候选，分别使用官方 scorer 评分并对比逐题改善与退步。
该测试是单轮题上的增强决策实验，不覆盖完整 ReAct 或 BFCL 多轮环境。内部复核不是赛后裁判。

结果保存在 `benchmark_runs/bfcl/`，含官方 result/score、命令日志和统一报告。
它是 subset 结果，不能冒充 BFCL 官方 full leaderboard overall。当前项目的
`AGENT_API_KEY/deepseek-flash` 不会自动映射成 BFCL 官方 DeepSeek 配置。

查看成绩不会调用模型，也不会产生费用：

```bash
# 最近一次已完成的运行
python3 show_bfcl_score.py

# 列出所有历史运行，复制需要查看的运行 ID
python3 show_bfcl_score.py --list

# 精确查看某次运行；也可以传运行目录或报告文件路径
python3 show_bfcl_score.py --run <运行ID>

# 查看最近一个运行（包括尚未完成）的分组进度
python3 show_bfcl_score.py --progress

# 查看指定运行的分组进度
python3 show_bfcl_score.py --progress --run <运行ID>

# 机器可读输出
python3 show_bfcl_score.py --run <运行ID> --json
```

完整命令、报告字段和边界见 [benchmarks/README.md](benchmarks/README.md)。

## 三个刻意的设计选择

1. **`finish` 是工具，不是一句"我说完了"**
   让模型自己判断"任务完成"最不可靠。做成工具调用，编排层就有了明确的、可计数、可拦截的终止信号。

2. **工具失败也回填给模型**
   `except` 里不 raise，而是把 `ERROR: FileNotFoundError: ...` 作为 observation 塞回去。模型下一轮看到报错，通常会改策略。这是最便宜的自我纠错。

3. **声明和执行分离**
   `TOOL_SPECS` 描述工具，`TOOL_IMPL` 提供本地实现，`ToolRegistry` 验证并执行；`TOOLS` 由声明生成。

## 试过就知道了的坑

- **把 `--steps` 调到 2**，看它在预算耗尽时是什么状态：不是失败，是"被截断"。
- **把 `--budget` 调到 1500**，观察 `[记忆]` 那几行的压缩过程。
- **注释掉 `safe_path` 的检查**，让它读 `../../etc/hosts` 试试。真实智能体的危险就来自这里。
- **给工具声明增加一个未绑定实现的工具**，看 registry 如何在运行前拒绝不一致的配置。

## 测试与 CI

全部离线测试（模拟模型和 HTTP 响应，不访问网络、不需要 API key）：

```bash
./run_tests.sh
```

它依次运行 210 项标准库核心测试，覆盖增强决策、工具契约、暂停与恢复、BFCL、事件、会话、赛后评测、主循环和 token 计量。
原有测试曾在 Python 3.9 和 3.12 上验证；本次加入研究模块后使用 Python 3.13.2 完成离线测试。GitHub Actions 配置为对 Python 3.9–3.12 执行同一脚本，本次尚未验证 CI 结果。

只看会话部分：

```bash
python3 -B -m unittest -v test_session
```

主循环的调度流程、模块职责及兼容边界见 [Agent 主循环](docs/AGENT_LOOP.md)。

## Research Agent：本地资料与长期记忆（第一版）

研究模式复用 Astrion 的 Agent Loop、工具契约和事件。主 Agent 可以用 `run_workers` 并发委派独立子任务，或用 `run_worker` 顺序委派；子 Agent 没有再次委派工具。资料和项目记忆按项目区分，对话记忆在所有项目可检索。主子 Agent 均有资料/联网工具及当前项目产物与隔离执行工具；长期记忆仍由用户显式写入。

在 `astrion-agent/` 目录执行：

```bash
# 创建项目（返回 project_id，以下示例假设是 1）
python3 -m research project-add "Agent 研究"
python3 -m research projects

# 导入现有 UTF-8 .md 或 .txt 资料（替换为自己的路径）
python3 -m research document-add --project 1 /path/to/paper-notes.md
python3 -m research search --project 1 "Agent 工具调用"

# 显式写入长期记忆，分别属于全局对话和某个项目
python3 -m research memory-add --scope conversation "请先讨论再修改代码"
python3 -m research memory-add --scope project --project 1 "比较单 Agent 与多 Agent"
python3 -m research memories --project 1
# 将一条记忆标为失效，保留其原文；不是彻底擦除
python3 -m research memory-forget 1

# 真实模型请求，使用已有 .env；运行事件会包含资料和模型输出
python3 -m research run --project 1 --trace research_data/run-001.json "比较资料中的观点并标注来源"
```

默认 SQLite 文件为当前目录下 `research_data/astrion.sqlite3`；可在子命令前用 `--db 路径` 指定其他数据库。默认目录在 Git 中被忽略。资料以文本快照、来源路径和 SHA-256 保存；同一项目内同名资料不会自动覆盖。记忆失效是软删除，数据库仍保留原文；没有实现彻底删除、编辑、加密、访问用户身份或备份恢复。

关键词检索保留标准库实现，并可选启用本地多语言语义检索；PDF/OCR、联网文献/DOI/网页读取已接入。`read_knowledge` 可以按文档 ID 和行号接着读取原文，限定当前项目、每次最多 80 行和 6000 字符；检索和阅读返回 `new_ranges` 帮助识别重复证据。运行结束后 `citation_audit` 仅检查 `[D文档ID:L起始-L结束]` 是否落在本次或续写继承的实际检索/阅读返回且未截断的资料范围内；它不判断引用是否支持结论，也不保证每个结论都有引用。 位置未覆盖也可能是报告里用作警示的反例；新版检查记录相应报告上下文供人工核对，不自动排除任何引用。同批子 Agent 并发运行，各自拥有模型客户端、数据库连接和上下文；结束后汇总用量，不重复累计。支持根上下文续写，尚无运行中子任务恢复或任务时间上限。事件和数据库可能包含私有资料，请自行管理存储和分享范围。可以通过 `--workers 0` 运行单 Agent 对照。

`read_knowledge` 接到超过 80 行的范围时，只返回当前最多 80 行，并给出 `next_page`；`selection_complete` 表示所请求范围是否完整返回。当前页超过 6000 字符时，先用返回的 `continuation` 通过 `read_document` 按字符补读，再跟随 `next_page`。行号超出文档末尾会明确标记，分页不会把尚未返回的原文当成已读。

新运行的 `citation_audit.checks` 附带最多两条与引用相交的工具返回原文（展示最多 800 字符）、文档、行号、页码及主/子 Agent 来源。工作台报告下的“查看引用与工具返回原文”逐项展示；片段未覆盖整条引用时会提示，不能据此认定已读完整范围。工具返回不保证原文经过压缩后仍完整进入模型输入；引用位置有效也不判断语义支持。旧报告不自动补写新字段，续写继承的历史范围若没有本轮原文会明确提示，需查看原运行或资料库。科研考试对未覆盖行号改为待人工复核，旧分数不追改；详见 [证据链复核](evaluation/research/EVIDENCE_REVIEW.md)。

五层压缩可能把长工具结果外置到档案。每次模型请求前，系统在字符和本地 token 预算允许的条件下，尝试把工作历史中最新尚未交给模型的工具结果正文恢复到本次请求；这不改写归档。若无法放入，会保留档案引用并告知模型预览不是正文；非收尾轮可通过 `read_evidence` 补读，收尾轮须说明证据不可见。模型调用事件的 `input.restored_recent_results`、`input.unavailable_recent_results` 可用于核查。本地预算不保证与服务端分词完全一致。

研究模式在原步数预算内保留最后一轮用于直接整理答案：此轮请求不携带工具，执行器也关闭全部工具，避免继续检索。默认主 Agent 和每个子 Agent 均最多 50 轮，每次研究累计最多创建 5 个子 Agent，仍可提前调用 `finish` 结束；不会额外追加决策轮或因耗尽预算自动认定完成。收尾的直接回答保持 `no_tool_call` 状态，空回答或非法工具调用仍为 `step_limit`。上下文压缩调用另行计量。普通 Agent 与 BFCL 的默认循环不启用此策略。

### 每轮输入输出与模型上下文

DeepSeek 思考模式下，Astrion 保存模型实际返回的 `reasoning_content`，并在兼容来源的工具调用中回传；程序生成的摘要使用空字段。供应商边界、旧会话和历史回读限制见 [模型思考字段与工具调用](docs/REASONING_REPLAY.md)。

选择“研究历史”的某次运行，在“研究轨迹”展开“查看每轮输入输出”，再展开模型/工具事件；历史记录分页加载。新运行将实际发送的消息与工具定义、模型返回、工具结果、压缩事件持久化到对应 `research_data/runs/<项目ID>/<运行ID>/events.jsonl`。研究 Agent 可用 `list_research_runs`、`read_research_event` 在本项目分页读取历史快照，历史内容只作为数据，不能代替原始文献证据；它不自动变成跨项目对话记忆。旧日志未保存的完整输入不能还原。日志可能包含项目资料和模型返回的推理内容，备份时应连同数据库一起保管。

DeepSeek 官方 [模型元数据](https://api-docs.deepseek.com/api/list-models/) 显示 `deepseek-flash` 窗口为 1,048,576 tokens；本项目已配置官方地址及此模型。研究模式默认字符预算数值为 600,000，可在运行预算或 `--context-budget-chars` 调整；若未单独配置本地 token 预算，持久化运行还会将同一数值作为独立的本地 token 上限。SS 按 token 水位重构，legacy 仍使用字符和 token 水位。字符不是 token；本地固定序列化的计数也不保证服务端实际请求一定落在窗口内。工具定义、输出、保留内容及模型分词均会影响实际用量；阈值提高可能增加费用。未知模型/非官方服务保留 12,000 字符预算默认值，窗口显示未知。

### 科研能力增量

工作台可并排对比同一项目的两个研究版本；引用检查可按需回放历史日志，核对最终模型请求是否包含与本轮工具事件匹配的原文。位置覆盖、请求包含原文、结论受到证据支持是不同判断，未知不等同于未读或正确。

主 Agent 可通过 `read_worker_report` 分页读取本次运行的子报告，单页最多 4000 字；报告产物保存失败时，运行内仍可读取。子报告须回到原始资料核实。工具白名单未开放时无法使用，进程结束后临时分页不可用。详细契约、旧记录限制和验证见 [三轮科研能力迭代](docs/RESEARCH_EVOLUTION.md)。

### 研究报告可以多次续写

工作台选择“研究历史”中的版本，输入补充要求，点击“继续研究”；新版本生成后可以继续补充，旧版本可随时回看。选择“新建研究”才启动独立任务。

历史列表会标注运行状态；选择失败运行或没有报告的步数耗尽运行，可点击“清理失败记录”，确认后删除该运行的结果和事件日志。存在后续版本、正在被续写、存在未知文件或无法确认依赖时，后端拒绝清理。清理不会删除项目资料、共享上下文归档或产物，也不是彻底擦除；删除后不能再从该记录恢复。并发保护依赖新版本的文件锁，请等旧版进程中的运行结束并重启后端及相关 CLI 进程后使用。项目范围接口为 `DELETE /api/projects/{project_id}/runs/{run_id}`。

```bash
.venv-ui/bin/python -m research runs --project 1
# 将 research_... 替换为历史列表中的完整 run_id
.venv-ui/bin/python -m research run --project 1 --continue-run research_... \
  "继续阅读上次未覆盖的文献，保留有效内容，形成带引用的完整修订版"
```

失败运行若有完整检查点，也可在研究历史中选择并继续；检查点后的操作可能需要重做，已有产物应先核对。每次续写新建 run_id 和 context scope，继承用户原话、已读证据、固定原句和任务计划。旧报告是待核查草稿；不会把旧报告中的引用直接算成已读证据。各版独立记录耗时、token、子任务和产物 ID，预算重新计数。主/子轮数及子 Agent 数可通过 `--steps`、`--worker-steps`、`--workers` 或工作台预算控件下调。详情见 [续写与并发说明](docs/RESEARCH_CONTINUATION.md)。

### 科研考试 v1

基于本地 25 篇论文原件建立 20 题和开发/保留集；每次研究使用隔离资料库，禁止外部来源工具，并由独立程序和可选模型裁判赛后评估。标准答案草案仍需人工复核，不能把位置有效当作语义正确。试题、运行与复核流程见 [科研考试说明](evaluation/research/README.md)。

## Research 工作台（Vue 3）

本地工作台使用 Vue 3、Bootstrap 和 FastAPI，提供项目、资料、长期记忆与研究界面。研究过程通过 SSE 传递主 Agent 和子 Agent 运行事件；OpenAI 兼容的模型接口以流式方式返回内容和 `finish` 答案片段。生成中的内容是暂定的，最终以任务结束事件的结果为准。支持情况取决于实际模型服务的 Chat Completions 流式协议。

研究轨迹显示当前步数、检索与阅读片段数和收尾阶段；报告区区分完成、需要补充信息、步数耗尽与请求失败。`step_limit` 的空答案不会再显示为成功，也不会用最后一段过程输出冒充最终报告。

研究报告支持 Markdown 标题、强调、列表、表格、引用、代码块与链接；模型增量到达时同步渲染，最终答案使用同一渲染器。采用 markdown-it 解析，再经 DOMPurify 标签/属性白名单过滤；原始 HTML 不执行，外部链接使用隔离的新窗口，图片只显示说明。排版保持紧凑。开发服务器启动后，可打开 `/tests/markdown.html` 运行格式、增量内容与危险输入的浏览器回归检查。

首次启动后端（在 `astrion-agent/`）：

```bash
python3 -m venv .venv-ui
.venv-ui/bin/python -m pip install -r requirements-ui.txt
.venv-ui/bin/python -m uvicorn research.api:app --host 127.0.0.1 --port 8000
```

另开终端启动前端（需要 Node.js 18 或更高版本；若安装了 nvm，可先执行 `nvm use 18`）：

```bash
cd frontend
npm ci --cache ../.npm-cache
npm run dev
```

打开前端命令显示的本地地址。开发服务器把 `/api` 代理到本机 8000 端口；API key 沿用项目已有 `.env`，由后端读取，不发送给浏览器。后端数据库固定默认为 `astrion-agent/research_data/astrion.sqlite3`，可通过 `ASTRION_RESEARCH_DB` 指定。后端默认仅按上面的命令监听本机，不提供用户认证，不适合公开部署。

PDF 已支持；联网检索见分阶段科研工作流。支持从研究历史选择已结束运行，输入补充要求后继续研究并另存新版本；暂不支持流中途取消和崩溃现场恢复；浏览器断开后，后台任务可能仍继续并产生模型费用。上下文原始日志与完整批次检查点现已写入 `context.sqlite3`，完整事件轨迹及结果另存到 runs 目录；页面刷新后可从研究历史重新加载报告及其续写上下文。资料与记忆管理仍沿用本地项目隔离和软失效语义。未使用真实模型验证时，离线测试不代表任意模型供应商都兼容流式格式。

仅运行流式拼接离线测试：`python3 -m unittest test_streaming`。接口和本机 HTTP 联调测试还需安装 `requirements-ui-test.txt`，运行 `.venv-ui/bin/python -m unittest test_research_api test_research_http`。

## 当前已知边界

1. **研究模式的两层 Agent Tree。** `research` 支持最多 5 个子 Agent 并发，保留顺序委派；子 Agent 不能继续委派。续写继承根 Agent 检查点，子 Agent 按需重新创建；取消、运行中树级恢复和通用多 Agent 编排尚未实现。
2. **`completed` 不是验收。** 它只表示模型调用了有效 `finish`，不保证文件内容正确；结果正确性要靠 `evaluation/` 的裁判。
3. **字符预算不是 token 硬上限。** 持久化运行另有本地 token 输入预算和发送前检查，但本地固定序列化并非服务端隐藏模板，不能保证服务端实际 token 数。非持久化调用若要启用该检查，应显式配置本地 token 预算。
4. **会话恢复不是事务。** 保存点在每轮结束；若在模型请求或一批工具执行中途终止，最近一轮会重做，工具可能被重复执行。
5. **会话和报告包含数据。** 模型消息、工具输出和文件快照可能含敏感内容；分享前要检查，默认目录虽被 git 忽略，不代表内容可以公开。
6. **沙箱不是操作系统隔离。** `safe_path()` 仅限制基础 CLI 文件工具，不是容器级安全边界；研究模式 Python 则只能使用 Docker 适配器，环境不可用就拒绝执行。
7. **没有任务时间上限。** 只记录耗时；单次 HTTP 超时 120 秒，`--steps` 限制决策轮数。
8. **BFCL 默认基准是固定 200 条单轮子集。** 五个核心类别各 40 条、等间隔确定性抽样；官方 handler 另有显式 `all-scoring` 入口。Astrion 单轮适配器只支持固定 ID 的 3/20/200 条子集，不能覆盖多轮、memory、live 或 web-search 类别。
9. **ToolBench 尚未接入。** 本阶段没有安装或调用 ToolBench/StableToolBench，也不把它们的分数与 BFCL 混合。
10. **Event v1 还不是恢复事实源。** 它是只追加的旁观日志；session 仍按完整消息轮次保存，不能从事件重放副作用或恢复运行。
11. **清单和工具适用性依赖模型判断。** 清单可能漏项或被错误标为完成；工具描述与提示词不提供语义正确性保证。目前计算工具有项目作用域和执行超时，尚无多用户权限或通用人工批准机制。

## 想继续加什么（按难度）

1. 把 ReAct 循环显式化为可检查的决策状态机，并保持 Event v1 行为可对比
2. 让评测裁判按统一事件分析轨迹、效率、稳定性和安全性
3. 讨论 Event 与 session checkpoint 的关联方式，但暂不直接承诺可重放副作用
4. 提炼 `ToolSpec` / `ToolCall` / `ToolResult` 契约，为 MCP 适配做准备
5. 以 README 中固化的 BFCL 200 baseline 为对照，重复测量并优化 Astrion 的工具决策质量与输出效率
6. 会话文件的轮转、加密或按项目分目录管理
7. 扩展研究模式：评估知识检索和引用质量、完善长期记忆，再按技术愿景探索 MCP、CodeAct 和 Computer Use

### 结论与证据的显式关联

研究主 Agent 和子 Agent 可以用 `record_claim` 将逐字结论与本 Agent 档案中 `read_knowledge` 等工具返回的完整正文观察、连续原文关联；工具由存档计算文档版本、页码和行号，`list_claims` 分页取回。各 Agent 的档案隔离，根 Agent 需要自己读取证据才能登记自己的结论。续写继承主 Agent 的登记和档案；五层压缩保留登记标识。旧运行不会自动补造这一数据。

报告生成后，系统还会在最多 4 个受预算限制的批次中，尝试从最终报告与该 Agent 已归档、未截断的原文中补登记；模型提出的结论和引文须分别与报告及原文逐字匹配，无法覆盖的内容保持未知。补登记会额外调用模型并产生费用，调用失败不会伪装成已覆盖。随后分别记录 **引用位置关联** 和 **模型语义初审**（支持、部分支持、不支持、无法判断），工作台可展开查看。模型初审使用周围已读取正文和提取警告，且仅是可能出错的初筛，不是人工复核或事实正确性证明；资料抽取或正文不完整时须回到原文。只有登记且逐字出现在报告中的结论被检查，其他结论的覆盖率未知。初审结果与输入保存在运行 JSON 的 `claim_audit`，其中 `registration.usage` 和 `review.usage` 分别记录补登记与初审的 token；`usage_including_root_review` 包含主 Agent 的补登记和初审，旧 `usage` 保持其原字段位置（包含子 Agent 的补登记与初审）。 工作台优先显示包含审查的总 token；旧记录没有该字段时标为研究阶段用量。默认一次至多 20 条登记，每条最多 3 个来源；来源必须是本 Agent 已归档且未截断的文档观察，单次观察不代表全文。

[验收候选集与复核方法](evaluation/research/acceptance_v2/README.md)包含 11 题、20 个检查项；当前仅为辅助核对，尚待独立人工审定，不可作为已验证的金标准或直接宣称通过率。

结论引用关联会检查结论末尾的内部引用和紧邻引用；相邻引用块若只能确定是候选关系，则保持待核对。同一句原文仅在**同 Agent、同原件及提取文本版本、引用范围内完整读取且可逐字唯一核验**时可关联到另一位置；不同内容不通过模糊匹配自动替换。审查时，登记原文和报告引用的已读原文会分别提供给模型；缺失、截断或无法核实版本时保持无法判断。历史报告可使用 `python -m evaluation.research.replay_claim_audit --report <原报告> --output <新文件>` 在归档副本中重放，追加 `--model-review` 才调用模型；详细说明和示例见 [回放记录](evaluation/research/acceptance_v2/CLAIM_CHECK_RESULTS.md)。

引用的整个行号范围必须被同一版本、未截断的已读观察完整覆盖；相邻观察可逐行核对并拼接，有缺口、版本不明或重叠内容冲突时保留待核对。审查上下文包括同版本已读的相邻行（每侧最多 80 行、总上下文最多 8000 字符），并记录实际上下文行号、局部截断、窗口限制和冲突；未读范围不能由模型假定。多句或分号连接的复杂结论可能被保守地标记为待核对。

### 研究版本关系图

在研究工作台选择项目后，展开“查看研究版本关系图”。每个节点代表一次保存的研究运行，箭头仅来自实际记录的 `parent_run_id`；点击节点可打开该版本报告，再输入补充要求继续研究。每次续写保留旧版本并创建新运行，也可以从同一旧版本产生多个分支。图只展示跨版本关系，不把同一次运行里的工具调用、子 Agent 任务或时间先后假定为依赖。

图最多展示最近 500 条；更早的版本可通过研究历史分页查看。无法读取或不属于当前数据库的记录会计数提示；父版本缺失、截断或关系异常不会伪造连线。节点显示“有恢复记录”只表示检查点存在且通过基本结构检查，真正续写前仍会完整校验。失败版本若检查点可用也可尝试续写；检查点后的操作可能需要重做。对应只读接口为 `GET /api/projects/{project_id}/runs/graph?limit=200`，limit 范围 1–500。

### 可选委派路由

研究工作台的“委派方式”默认仍为原有主 Agent 自主决策（`baseline`）。选择独立路由（`structured`）后，系统额外调用一次模型，判断是否存在可独立进行的本地资料研究，并验证结构化决策及子任务数量；通过后复用现有子 Agent 池并发执行。预派发的资料工具被程序限制为本地读取，不能调用联网、代码执行或写入产物的工具；系统仍会保存日志、上下文与子报告。主 Agent 必须将子报告视为待核查数据，不能把它当作原文证据。路由失败或条件不足时回到原有主循环，不计为主 Agent 决策轮数。实际路由、子任务和用量记录在研究轨迹与结果的 `delegation_routing` 中；结果的总用量包含路由及子 Agent 调用。

由于续写可能包含多轮新增约束，当前续写会跳过预派发，由拥有完整历史的主 Agent 自主决定是否委派；历史子报告保留，但旧 worker ID 不能作为本轮读取句柄。独立路由会额外耗费 token 和时间，不能仅凭更高委派率认定质量改善，应进行同题、同模型和同资源预算对照并人工核查结论。

派发前按主 Agent 消息与工具定义预留交接空间；交接超预算时退化为可分页读取的子报告指针。后续上下文增长时也会检查并缩减可选交接，完整路由记录保存在研究轨迹中；这不能保证其他上下文内容超预算时一定能继续。模型失败且未返回用量时标为未知，供应商内部 HTTP 重试可能产生无法确认的费用，因此已知 token 总数可能只是下限。

```bash
# 在项目目录使用已有项目和模型配置
python3 -m research run --project 1 --delegation-mode structured "比较项目资料中的两种方法"
# 使用相同验收题对照（需要配置可用模型）
python3 -m evaluation.research.acceptance_v2 compare-routing --case R01 --mode multi --steps 8 --workers 2 --worker-steps 4 --budget-chars 60000 --output research_data/research_eval_v1/router_compare
```
