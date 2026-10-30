# toy-agent

一个能完整看懂的极简智能体：**ReAct 循环 + 四个工具 + 沙箱 + 独立的记忆模块**。
全部标准库，没有第三方依赖。

```
toy-agent/
├── agent.py        循环、工具、模型客户端
├── memory.py       会话记忆：消息窗口 + 超额压缩
├── evaluation/     赛后评测：任务、运行器、可注册裁判
└── sandbox/        它唯一被允许读写的地方
    ├── data.txt    演示输入
    └── result.txt  运行产物
```

## 跑起来

需要一个 OpenAI 兼容的 API key，写在上一级的 `Multi Agent/.env` 里（见下）。

```bash
python3 agent.py "读取 data.txt，算出所有数字的和，写入 result.txt"
python3 agent.py                      # 不传任务就走默认任务
```

没有 key 时会直接报错退出，**没有离线兜底**——宁可失败，也不要给你一个假装在思考的假模型。

常用参数：

| 参数 | 作用 |
|---|---|
| `--steps N` | 最大步数，默认 10 |
| `--budget N` | 记忆预算（字符），默认 12000，超出后压缩最旧的轮次 |
| `--model M` | 临时换模型，覆盖 `.env` |
| `--base-url U` | 临时换服务端，覆盖 `.env` |

## 配置

密钥放在上一级的 `.env`，不用每次 export：

```
Multi Agent/
├── .env              ← AGENT_API_KEY 写这里
└── toy-agent/
```

`agent.py` 从自己目录向上找 `.env`（最多 3 层，越近越优先）。规则是**已有的环境变量不被覆盖**，所以 shell 里临时 `export` 的值总是赢。

优先级：`--model/--base-url` > 环境变量 > `.env`。

## 赛后评测

评测层把任务交给 agent，等运行结束后再让裁判查看运行记录。默认任务在
`evaluation/cases.json`：提供 `data.txt` 的初始内容，要求 agent 写入求和结果，
由 `file_equals` 裁判检查 `result.txt`。

从 `toy-agent/` 目录运行：

```bash
python3 -m evaluation --case sum_001
```

这会使用与 `agent.py` 相同的 API 配置，调用真实模型。运行日志输出到 stderr；
stdout 是 JSON 报告，可以按需重定向到文件：

```bash
python3 -m evaluation --case sum_001 > evaluation-result.json
```

报告会包含任务内容、初始文件、模型可见的消息和工具结果；评测器不会主动记录连接 API 的 key。
保存或分享报告时应留意这些内容可能包含任务数据。默认不写报告文件。

其他常用参数：`--cases` 指定任务 JSON 文件，省略 `--case` 时顺序运行所有任务；
`--steps`、`--budget`、`--model`、`--base-url` 分别覆盖 agent 的运行配置。

一次评测分为：

1. **准备任务**：为本次任务创建临时沙箱，只写入 `initial_files`。裁判参考答案保存在任务配置中，不随任务消息发给模型。
2. **运行 agent**：记录模型决策、摘要请求与结果、工具结果和结束状态；运行记录与可压缩的 `Memory` 分开。
3. **保存运行事实**：统计 `run()` 的耗时、已知 token 用量、最终文件快照和运行状态，然后清理临时沙箱。
4. **赛后评判**：每个已注册裁判获取任务和运行记录的独立副本，返回自己的结论。多个裁判的结论分别保留，不强行合成总分。

运行时间使用 `time.perf_counter()`，从调用 `run()` 前开始，到其返回或抛出异常为止；
它包括模型等待、工具执行、记忆压缩和同步事件记录，**不包括**准备环境、生成文件快照或裁判评测。
每个裁判的 `duration_seconds` 单独记录。目前只记录时间，**没有任务时间上限，也没有时间通过阈值**。
原有的 agent `--steps` 步数限制和单次 HTTP 请求的 120 秒超时仍然存在。

JSON 报告主要包含 `case`（完整评测配置）、`run`（`result`、`duration_seconds`、
`model`、`agent_config`、`token_usage`、`trace`、`artifacts` 和 `artifact_errors`）
与 `judgments`（各裁判的结果和耗时）。`run.result.status` 是运行状态；
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

离线验证（使用模拟模型和 HTTP 响应，不访问网络）：

```bash
python3 -B -m unittest -v test_evaluation test_run_lifecycle test_token_usage
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

`steps` 是主循环中已完成的模型决策调用次数，不包含用于记忆压缩的摘要调用。
程序也会在终端打印 `[状态]`。例如，模型调用 `finish` 并不证明目标文件一定正确，
它表达的是模型给出的完成信号。

每条工具调用都要有对应的结果。有效的 `finish` 会先写入一条 `tool` 消息，再结束。
如果同一批调用中还有排在 `finish` 后面的工具，程序不执行它们，并写入“未执行”的结果。
非法 JSON、非对象参数，以及无效的 `finish.answer` 都会变成工具错误消息，交给下一轮模型；
如果已到达步数上限，则按 `step_limit` 结束。

离线验证主循环与 token 计量（模拟模型和 HTTP 响应，不访问网络）：

```bash
python3 -B -m unittest -v test_run_lifecycle test_token_usage
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
- **累计范围是一个 `ChatLLM` 实例。** 命令行每次启动创建一个新实例，从零累计；没有持久化到磁盘。
- **计量与预算分开。** 现有 `--budget` 仍然是字符预算，用于触发记忆压缩。
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
| 工具 schema | `agent.py` 的 `TOOLS` | 给模型看的说明书（JSON Schema） |
| 工具实现 | `list_files` 等 | 真正执行的函数 |
| 策略层 | `safe_path()` | 路径钉死在 sandbox，越界即抛错 |
| 模型层 | `ChatLLM` | 一个 `_post` 管所有 HTTP，`chat` 和 `summarize` 共用 |
| 编排层 | `run()` | 那个循环，全部精华在这 45 行 |
| 记忆层 | `memory.py` 的 `Memory` | 消息窗口 + 压缩 |
| 终止 | `finish` 工具 + `max_steps` | 显式终止 + 预算硬停 |

## 记忆系统（memory.py）

它只回答一个问题：**上下文快满了，该丢什么、怎么丢。**

```python
memory = Memory(SYSTEM_PROMPT, budget_chars=12000, keep_recent=4)
memory.add_user(task)
msg = llm.chat(memory.for_model())       # 组装成 API 的 messages
memory.add_assistant(msg)
memory.add_tool(call_id, observation)

if memory.needs_compaction():            # 超预算了
    memory.compact(llm.summarize)        # 把最旧的几轮压成摘要
```

三个关键点：

1. **压缩以「轮」为单位。** 一轮 = 一条 assistant + 它触发的所有 tool 结果。拆开就是非法消息，API 直接 400。`check()` 每次压缩后自动校验配对。
2. **保留轮数按预算动态收缩。** 固定保留 4 轮遇到大文件照样爆，所以会一路减到「压得进预算」为止，最少留 1 轮。
3. **它不知道模型的存在。** 摘要靠调用方传函数进来（`llm.summarize`），所以这个模块可以脱离网络单独测。

摘要放在 system 里（`## 你之前做过的事`），不新增 role——兼容性最好。

## 三个刻意的设计选择

1. **`finish` 是工具，不是一句"我说完了"**
   让模型自己判断"任务完成"最不可靠。做成工具调用，编排层就有了明确的、可计数、可拦截的终止信号。

2. **工具失败也回填给模型**
   `except` 里不 raise，而是把 `ERROR: FileNotFoundError: ...` 作为 observation 塞回去。模型下一轮看到报错，通常会改策略。这是最便宜的自我纠错。

3. **schema 和执行分离**
   `TOOLS` 是说明书，`TOOL_IMPL` 是执行体。中间那层就是将来放权限检查、日志、超时、缓存的位置。

## 试过就知道了的坑

- **把 `--steps` 调到 2**，看它在预算耗尽时是什么状态：不是失败，是"被截断"。
- **把 `--budget` 调到 1500**，观察 `[记忆]` 那几行的压缩过程。
- **注释掉 `safe_path` 的检查**，让它读 `../../etc/hosts` 试试。真实智能体的危险就来自这里。
- **给 `TOOLS` 加一个 schema 但不加实现**，看模型调用时会发生什么（答案是：一条看不懂的 `KeyError`）。

## 想继续加什么（按难度）

1. `delete_file` 工具 + 删除前的确认钩子（体验一次策略层）
2. 记忆持久化到 `session.json`，支持中断后恢复
3. 继续完善结束状态，例如明确表示模型请求失败，并根据任务需要检查结果
4. 一个 `run_worker` 工具，内部再起一个 `run()`（多智能体，本质只是嵌套）
