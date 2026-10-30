# benchmarks

外部基准测试的适配层。它和 `evaluation/` 分开：

- `evaluation/`：评测 **sandbox 文件任务**，检查运行后的文件；
- `benchmarks/`：把 **case 自己的 messages + tools** 交给模型，记录原始响应；
  对错由 BFCL/ToolBench 的官方规则或 scorer 判断。

当前状态：**BFCL V4 已接入官方 harness；ToolBench 尚未接入。**
`common.py` 仍保留为通用原始响应适配层，正式 BFCL 运行走 `bfcl.py`。

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

## BFCL 正式集成

正式路径固定使用 `bfcl-eval==2026.3.23`，并在独立 `.venv-bfcl/` 中安装。
toy-agent 不复制 BFCL 数据、prompt、model handler 或 scorer，只调用官方 CLI：

```text
benchmarks/bfcl.py
  → bfcl generate --run-ids
  → bfcl evaluate --partial-eval
  → 读取官方 result/score
  → 保存 bfcl_run_report.json
```

### 1. 安装隔离环境

官方包要求 Python 3.10 以上。本项目默认用本机的 Python 3.12：

```bash
cd '/Users/apple/Desktop/科研/Multi Agent/toy-agent'
./setup_bfcl.sh
```

这个环境是可选旁路依赖，不改变 toy-agent 核心的标准库运行方式；`.venv-bfcl/`
和运行结果均被 Git 忽略。

### 2. 配置官方模型凭据

默认模型是 BFCL 官方注册的 `DeepSeek-V3.2-Exp-FC`。官方 handler 固定访问
DeepSeek 官方 endpoint，并读取 `DEEPSEEK_API_KEY`。可以把它写在项目根目录
`/Users/apple/Desktop/科研/Multi Agent/.env`：

```dotenv
DEEPSEEK_API_KEY=你的_DeepSeek_官方_key
```

`python3 -m benchmarks bfcl doctor` 和真实 `run` 会复用 toy-agent 已有的标准库
`.env` 加载器；已有 shell 环境变量优先。`--dry-run` 不读取 `.env`。
`.env` 已被 Git 忽略，不要把真实值写入文档、日志或版本库。

项目现有的 `AGENT_API_KEY` 不会被自动改名或转发。`AGENT_BASE_URL` 和
`AGENT_MODEL=deepseek-flash` 也不等于 BFCL 官方 DeepSeek 配置，不能据此冒充官方结果。
换其他 BFCL 官方模型时，同时把 `--model` 和 `--credential-env` 换成对应值。

### 3. 环境检查与 dry-run

```bash
# 检查固定版本、官方模型注册和凭据；不会调用模型
python3 -m benchmarks bfcl doctor

# 只生成本次目录、20 个 ID 和两条官方命令；不要求 BFCL 已安装
python3 -m benchmarks bfcl run --dry-run
```

`doctor` 要求版本严格等于 `2026.3.23`，避免数据、prompt 和 scorer 漂移。
适配器兼容该版本 `bfcl version` 在部分安装中的 distribution 名问题，会退回到同一
venv 的 Python 查询 `bfcl-eval` 元数据。

### 4. 运行官方 20 样例子集

```bash
python3 -m benchmarks bfcl run
```

固定 manifest 位于 `benchmarks/manifests/bfcl_v4_single_turn_smoke.json`，包含：

- `simple_python` 4 个；
- `multiple` 4 个；
- `parallel` 4 个；
- `parallel_multiple` 4 个；
- `irrelevance` 4 个。

每次创建独立的 `benchmark_runs/bfcl/<run-id>/`。其中包括官方要求的
`test_case_ids_to_generate.json`、官方 `result/`、官方 `score/`、命令日志和
`bfcl_run_report.json`。命令不经过 shell 拼接，凭据值不会写入统一报告。

统一报告中的 `official_category_scores` 直接读取官方 score JSON 首行的
`accuracy`、`correct_count` 和 `total_count`；单条对错不由本项目重新判断。
`derived_subset_micro_accuracy` 只是对这 20 个样例按数量汇总，明确属于
**subset**，不能当作 BFCL 官方 full leaderboard overall。

生成失败时不运行评测；评测失败时仍保留已生成的官方结果和日志。报告的状态可为
`completed`、`dry_run`、`setup_error`、`generation_error`、
`evaluation_error` 或 `orchestration_error`。

BFCL 官方资料：

- <https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard>
- <https://pypi.org/project/bfcl-eval/>
- <https://gorilla.cs.berkeley.edu/leaderboard.html>

## ToolBench（后置）

本阶段没有安装、调用或适配 ToolBench/StableToolBench。相关讨论保留为后续方向，
不会把它的依赖、报告或分数混进 BFCL。

- <https://github.com/OpenBMB/ToolBench>
- <https://github.com/zhichengg/StableToolBench>

## 结果和版本

`benchmark_runs/` 和 `benchmark_data/` 默认不纳入 git。BFCL 统一报告会记录：

```text
toy-agent commit / manifest / case ids
model / temperature / 线程数
BFCL V4 / bfcl-eval 固定版本
官方 result/score 路径与 SHA-256
```

不要拿 toy-agent eval 的分数、BFCL 20 样例 subset 和官方 full leaderboard 分数
直接比较；它们用的是不同任务、profile 和覆盖率。
