# benchmarks

外部基准测试的适配层。它和 `evaluation/` 分开：

- `evaluation/`：评测 **sandbox 文件任务**，检查运行后的文件；
- `benchmarks/`：把 **case 自己的 messages + tools** 交给模型，记录原始响应；
  对错由 BFCL/ToolBench 的官方规则或 scorer 判断。

**默认 agent 的行为不经过这里。** `agent.run()`、`SYSTEM_PROMPT` 和 `TOOLS`
保持不变；这里只使用 `ChatLLM.chat(messages, tools=...)` 这个可选接缝。

## 通用 case 格式

```json
[
  {
    "id": "simple_0",
    "category": "simple",
    "messages": [
      {"role": "system", "content": "你是函数调用助手。"},
      {"role": "user", "content": "北京现在天气怎么样？"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "查询城市天气",
          "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
          }
        }
      }
    ],
    "metadata": {"source": "example"}
  }
]
```

一个 case 只表示“要发给模型什么”，不包含参考答案。参考答案、AST 校验、
多轮状态机等属于具体 benchmark 的 scorer。

## Python 接口

```python
import agent
from benchmarks import load_cases, run_cases, summarize, write_jsonl

llm = agent.ChatLLM(key="...", base_url="...", model="...")

def chat(messages, tools):
    return llm.chat(messages, tools=tools)

cases = load_cases("cases.json")
records = run_cases(cases, chat, limit=20, model=llm.model)
write_jsonl(records, "benchmark_runs/example.jsonl")
print(summarize(records))
```

`run_cases()` 默认在某条 case 出错时记录 `error` 并继续；设置
`continue_on_error=False` 可以让异常直接抛出。

## BFCL 适配

BFCL 的官方数据、prompt 格式和 AST/state transition 评分都很具体。建议：

1. 从官方仓库或 `bfcl-eval` 读取数据，转换成上面的 `FunctionCallCase`；
2. 使用官方 system prompt、官方工具顺序，不要改成 toy-agent 的 `SYSTEM_PROMPT`；
3. 把模型原始响应交给官方 scorer，不要自己重写 AST 比较；
4. 先支持 `simple`、`multiple`、`parallel`、`parallel_multiple`、`irrelevance`，
   再考虑 multi-turn 和 V4 agentic（web search / memory）；
5. 报告里注明数据集版本、类别、case 数量，区分 **subset score** 和官方 overall。

BFCL 官方资料：

- <https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard>
- <https://pypi.org/project/bfcl-eval/>
- <https://gorilla.cs.berkeley.edu/leaderboard.html>

## ToolBench 适配

ToolBench 更重：它包含大规模 API 检索、规划和执行，完整链路依赖 RapidAPI
或模拟 API server。建议先使用 **StableToolBench** 的缓存响应和一个小样本，
先看 pass rate；win rate 依赖 LLM 裁判，成本和波动都更高，后置处理。

- <https://github.com/OpenBMB/ToolBench>
- <https://github.com/zhichengg/StableToolBench>

## 结果和版本

`benchmark_runs/` 和 `benchmark_data/` 默认不纳入 git。每份结果至少应能追溯：

```text
agent commit / prompt hash / tools hash
model / temperature / max_tokens
benchmark 数据集版本
harness / scorer 版本
```

不要拿 toy-agent eval 的分数、BFCL subset 的分数和官方 full leaderboard 分数
直接比较；它们用的是不同 profile 和不同覆盖率。
