# toy-agent

一个能完整看懂的极简智能体：**ReAct 循环 + 四个工具 + 沙箱 + 独立的记忆模块**。
全部标准库，没有第三方依赖。

```
toy-agent/
├── agent.py        循环、工具、模型客户端
├── memory.py       会话记忆：消息窗口 + 超额压缩
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
