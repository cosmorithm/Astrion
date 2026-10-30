# toy-agent

一个能完整看懂的极简智能体：**ReAct 循环 + 四个业务工具 + `finish` + 沙箱 + 独立记忆 + 可恢复会话**。
全部标准库，没有第三方依赖。

```
toy-agent/
├── agent.py        循环、工具、模型客户端、会话接入
├── memory.py       会话记忆：消息窗口 + 超额压缩 + 序列化
├── session.py      会话 JSON：原子保存、加载与恢复判断
├── benchmarks/     基准层：通用记录 + BFCL 官方 harness 旁路集成
├── evaluation/     赛后评测：任务、运行器、可注册裁判
├── run_tests.sh    全部离线测试入口
└── sandbox/        它唯一被允许读写的地方
    ├── data.txt    演示输入
    └── result.txt  运行产物
```

## 跑起来

需要一个 OpenAI 兼容的 API key，写在上一级的 `Multi Agent/.env` 里（见下）。

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
| `--budget N` | 记忆预算（字符），默认 12000，超出后压缩最旧的轮次 |
| `--model M` | 临时换模型，覆盖 `.env` |
| `--base-url U` | 临时换服务端，覆盖 `.env` |
| `--session PATH` | 会话 JSON 路径；存在时恢复记忆，运行中每轮自动保存 |
| `--fresh` | 忽略已有会话文件，从空记忆开始（需配合 `--session`） |

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

评测层把任务交给 agent，等运行结束后再让裁判查看运行记录。内置的 10 个任务在
`evaluation/cases.json`。例如 `sum_001` 提供 `data.txt` 的初始内容，要求 agent 写入求和结果，
由 `file_equals` 裁判检查 `result.txt`。

在 `toy-agent/` 目录中，先查看任务清单（无需 API key，也不会调用模型）：

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

从 `toy-agent/` 目录运行单个样例，或一次运行任务文件中的全部样例：

```bash
# 只运行一个任务
python3 -m evaluation --case sum_001

# 不传 --case：顺序运行默认 cases.json 的全部 10 个任务
python3 -m evaluation
```

这会使用与 `agent.py` 相同的 API 配置，调用真实模型。每个评测任务结束后会**自动保存一份 JSON 报告**到 `toy-agent/eval_reports/`，
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
2. **运行 agent**：记录模型决策、摘要请求与结果、工具结果和结束状态；运行记录与可压缩的 `Memory` 分开。
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
`model`、`agent_config`、`token_usage`、`trace`、`artifacts` 和 `artifact_errors`）、
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
- **累计范围是一个 `ChatLLM` 实例。** 命令行每次启动创建一个新实例，从零累计；token 用量没有持久化到磁盘，也不写进会话文件。
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
| 模型层 | `ChatLLM` | 一个 `_post` 管所有 HTTP，`chat` 支持可选 tools，`summarize` 共用 |
| 编排层 | `run()` | 那个循环，ReAct 的核心都在这里 |
| 记忆层 | `memory.py` 的 `Memory` | 消息窗口 + 压缩 + 序列化 |
| 持久化 | `session.py` | 会话 JSON 的原子保存、加载与恢复判断 |
| 基准层 | `benchmarks/` | 通用原始响应适配 + BFCL 官方旁路；默认 agent 不依赖 |
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

`Memory` 还提供 `to_dict()` / `from_dict()`，把系统提示、摘要、压缩次数和历史导出/恢复成普通字典；文件读写和任务元数据交给 `session.py`。

## 会话持久化（session.py）

`--session` 把记忆、任务、结束状态和累计步数写进一个 JSON 文件；默认不开启，保持原来的一次性运行行为。

```bash
python3 agent.py "读取 data.txt..." --session session.json
# 中断、崩溃或达到 --steps 后，重跑同一条命令即可继续
python3 agent.py "读取 data.txt..." --session session.json

# 忽略旧文件，从空记忆开始；第一次保存时覆盖
python3 agent.py "读取 data.txt..." --session session.json --fresh
```

规则：

- **保存点在每轮结束。** 只有一整批工具结果都写回 `Memory` 之后才保存，文件里的历史始终能通过 `check()`。如果进程在模型请求或一批工具执行中途终止，恢复点最多回退到上一轮，那一批工具可能重做。
- **原子写入。** 先写临时文件并 `fsync`，再用 `os.replace` 替换；不会留下半个 JSON。POSIX 下会话文件权限设为 600。
- **按任务判断是否继续。** 同一任务且上次状态是 `running` 或 `step_limit` 时，自动继续，不重复添加用户消息；其他情况会在已有记忆上追加新任务，不会自动继续旧任务。
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
- `benchmarks/bfcl.py`：调用固定版本的官方 `bfcl generate/evaluate`，导入官方分数；不重写 AST scorer。
- `benchmarks/manifests/bfcl_v4_single_turn_smoke.json`：固定 5 类、20 个 BFCL V4 样例。
- `setup_bfcl.sh`：在 `.venv-bfcl/` 安装可选依赖，不污染零依赖核心。

快速查看将要执行的正式命令，不安装 BFCL、也不调用模型：

```bash
python3 -m benchmarks bfcl run --dry-run
```

安装后，在项目根目录已有的 `.env` 中增加 BFCL 的专用密钥：

```bash
./setup_bfcl.sh
# /Users/apple/Desktop/科研/Multi Agent/.env
DEEPSEEK_API_KEY=你的_DeepSeek_官方_key
python3 -m benchmarks bfcl doctor
```

BFCL 命令会自动查找 `.env`，且不会覆盖 shell 中已经 `export` 的同名变量。
`.env` 已被 Git 忽略；不要把真实密钥写进 README、报告或提交记录。

运行固定 20 样例子集：

```bash
python3 -m benchmarks bfcl run
```

结果保存在 `benchmark_runs/bfcl/`，含官方 result/score、命令日志和统一报告。
它是 subset 结果，不能冒充 BFCL 官方 full leaderboard overall。当前项目的
`AGENT_API_KEY/deepseek-flash` 不会自动映射成 BFCL 官方 DeepSeek 配置。

完整命令、报告字段和边界见 [benchmarks/README.md](benchmarks/README.md)。

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

## 测试与 CI

全部离线测试（模拟模型和 HTTP 响应，不访问网络、不需要 API key）：

```bash
./run_tests.sh
```

它依次运行 96 项测试：契约 6 项、BFCL 旁路 13 项、通用基准适配 6 项、会话持久化 18 项、评测层 31 项、总报告 9 项、主循环 8 项、token 计量 5 项。
本地已在 Python 3.9 和 3.12 上验证通过；GitHub Actions 在 `.github/workflows/tests.yml` 中对 Python 3.9–3.12 执行同一脚本。

只看会话部分：

```bash
python3 -B -m unittest -v test_session
```

## 当前已知边界

1. **单 agent。** 尚未实现子 agent、任务分配、并发协作；`run_worker` 仍只是后续设想。
2. **`completed` 不是验收。** 它只表示模型调用了有效 `finish`，不保证文件内容正确；结果正确性要靠 `evaluation/` 的裁判。
3. **字符预算不是 token 硬上限。** `--budget` 按序列化字符数触发压缩；压缩后仍可能超预算，也没有请求前 token 估算。
4. **会话恢复不是事务。** 保存点在每轮结束；若在模型请求或一批工具执行中途终止，最近一轮会重做，工具可能被重复执行。
5. **会话和报告包含数据。** 模型消息、工具输出和文件快照可能含敏感内容；分享前要检查，默认目录虽被 git 忽略，不代表内容可以公开。
6. **沙箱不是操作系统隔离。** `safe_path()` 限制应用层文件工具；这不是容器级安全边界。
7. **没有任务时间上限。** 只记录耗时；单次 HTTP 超时 120 秒，`--steps` 限制决策轮数。
8. **BFCL 当前只是固定 20 样例的正式子集入口。** 已调用官方 harness 和 scorer，但尚未运行 full leaderboard、多轮或 V4 agentic；没有官方 DeepSeek 凭据时不能产生真实分数。
9. **ToolBench 尚未接入。** 本阶段没有安装或调用 ToolBench/StableToolBench，也不把它们的分数与 BFCL 混合。

## 想继续加什么（按难度）

1. `delete_file` 工具 + 删除前的确认钩子（体验一次策略层）
2. 继续完善结束状态，例如明确表示模型请求失败，并根据任务需要检查结果
3. 一个 `run_worker` 工具，内部再起一个 `run()`（多智能体，本质只是嵌套）
4. 会话文件的轮转、加密或按项目分目录管理
5. 在正式凭据可用后运行 BFCL 20 样例，分析官方 category score
6. BFCL 从单轮子集扩展到多轮，再决定是否进入 V4 agentic
7. ToolBench/StableToolBench 后置，单独讨论环境、成本和评分口径
