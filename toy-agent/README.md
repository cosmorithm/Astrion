# toy-agent

一个 300 行、零依赖（只用标准库）的最小智能体，用来**观察**智能体循环，而不是用来干活。

```
agent.py          全部代码：工具 + 模型客户端 + 循环
sandbox/          它唯一被允许读写的地方
  data.txt        演示数据：3, 1, 4, 1, 5, 9, 2, 6
  result.txt      跑完会生成，内容是 31
```

## 跑起来

```bash
# 离线演练：不需要 API key，用脚本假模型把循环走一遍
python3 agent.py --mock

# 换任务
python3 agent.py --mock "把 result.txt 删掉"      # 会撞上「没有删除工具」——这也是教学点

# 接真模型（OpenAI 兼容协议，DeepSeek / OpenAI / 通义都行）
export AGENT_API_KEY=sk-xxx
export AGENT_BASE_URL=https://api.deepseek.com   # 可省略
export AGENT_MODEL=deepseek-chat                 # 可省略
python3 agent.py "读取 data.txt，算出所有数字的和，写入 result.txt"
```

没设 `AGENT_API_KEY` 且没加 `--mock` 时，会自动退回离线模式并提示。

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
| `obs` | 工具的真实返回值，回填上下文 | `{"role": "tool", ...}` |

注意 `[2]` 的 think 引用了 `[1]` 的 obs——**这就是"循环"的全部含义**。

## 代码地图（按架构分层）

| 层 | 位置 | 说明 |
|---|---|---|
| 工具 schema | `TOOLS` | 给模型看的说明书（JSON Schema） |
| 工具实现 | `list_files` 等 | 真正执行的函数 |
| 策略层 | `safe_path()` | 路径钉死在 sandbox，越界即抛错 |
| 模型层 | `ChatLLM` / `MockLLM` | 同一个 `chat()` 接口，可互换 |
| 编排层 | `run()` | 那个 for 循环，全部精华在这 40 行 |
| 终止 | `finish` 工具 + `max_steps` | 显式终止 + 预算硬停，双保险 |

`ChatLLM` 和 `MockLLM` 共享同一个 `chat(messages) -> dict` 签名，这解释了为什么"换模型"在架构上是件小事——**模型层是可替换的零件，循环才是主体。**

## 三个刻意的设计选择

1. **`finish` 是一个工具，不是一句"我说完了"**
   让模型自己判断"任务完成"最不可靠。做成工具调用，编排层就有了一个明确的、可计数、可拦截的终止信号。

2. **工具失败也回填给模型**
   `except` 里不 raise，而是把 `ERROR: FileNotFoundError: ...` 作为 observation 塞回去。模型下一轮看到报错，通常会改策略。这是最便宜的自我纠错机制。

3. **schema 和执行分离**
   `TOOLS` 是说明书，`TOOL_IMPL` 是执行体。中间那一层就是将来放权限检查、日志、超时、缓存的钩子位置。

## 试过就知道了的坑

- **删掉 `max_steps`**，再看它会不会绕圈——这是"预算"存在的理由。
- **把 `safe_path` 的检查注释掉**，让它读 `../../etc/hosts` 试试。真实智能体的危险就来自这里。
- **给 mock 脚本加一步不存在的工具**，看编排层怎么把 `KeyError` 变成一个可恢复的 observation。
- **加 `--steps 2`**，看预算耗尽时它是什么状态：不是失败，是"被截断"。

## 想继续加什么（按难度）

1. `delete_file` 工具 + 删除前的确认钩子（体验一次策略层）
2. 对话历史持久化到 `session.json`，支持中断后恢复（状态层）
3. 超过 N 条消息时把早期对话压成摘要（上下文层，最难也最有价值）
4. 一个 `run_worker` 工具，内部再起一个 `run()`（多智能体，本质只是嵌套）

第 4 条做完你会发现：多智能体不是什么新东西，就是**把同一个循环套在另一个循环里，用上下文隔离换并行**。

## 关于 .env

密钥放在上一级的 `Multi Agent/.env`，不用每次 export：

```
Multi Agent/
├── .env              ← AGENT_API_KEY 写这里
└── toy-agent/
```

`agent.py` 会从自己的目录向上找 `.env`（最多 3 层，越近越优先），
规则是「已有的环境变量不被覆盖」——所以 shell 里临时 export 的值总是赢。

优先级：`--model/--base-url` 参数 > 环境变量 > `.env`。

`--mock` 时完全不读 key，可以放心在没网、没密钥的环境下演示。
