# benchmarks

外部基准测试的适配层。它和 `evaluation/` 分开：

- `evaluation/`：评测 **sandbox 文件任务**，检查运行后的文件；
- `benchmarks/`：把 **case 自己的 messages + tools** 交给模型，记录原始响应；
  对错由 BFCL/ToolBench 的官方规则或 scorer 判断。

当前状态：**BFCL V4 已接入官方 harness，并具备 Astrion 单轮决策适配路径；
ToolBench 尚未接入。** `common.py` 仍保留为通用原始响应适配层，BFCL 的两条
运行路径都由 `bfcl.py` 编排。

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

两条路径都固定使用 `bfcl-eval==2026.3.23`，并在独立 `.venv-bfcl/` 中安装：

- `bfcl run` 是官方模型 handler 基线；当前为了与 Astrion 严格对照，
  默认保留 `DeepSeek-V3.2-Exp-FC` registry/scorer，但把 handler 实际 API 请求模型覆盖为 `deepseek-flash`；
- `bfcl astrion-run` 用 Astrion 的 `ChatLLM` 生成响应和 Event v1，再把结果交给
  同一个官方 scorer。

Astrion Agent 不复制 BFCL 数据、prompt 或 scorer。官方基线路径是：

```text
benchmarks/bfcl.py
  → bfcl_official.py 临时覆盖 registry config 的 API model
  → 官方 bfcl generate --run-ids
  → bfcl evaluate --partial-eval
  → 读取官方 result/score
  → 保存 bfcl_run_report.json
```

Astrion 路径是：

```text
BFCL 官方 dataset + function schema
  → bfcl_astrion.py
  → Astrion ChatLLM.chat(messages, tools)
  → 每条 case 保存 Event v1
  → 写入 BFCL 官方 result JSONL
  → bfcl evaluate --partial-eval
  → 保存统一 bfcl_run_report.json
```

BFCL 单轮任务的目标就是预测函数名和参数，因此这条路径**不会真正执行 case 工具**，
也不会进入多步 `agent.run()` 循环。它测到的是 Astrion 的模型客户端、工具注入、响应
规范化、token 计量和事件记录，不代表 Memory、ReAct 多轮执行或本地工具运行能力。

### 1. 安装隔离环境

官方包要求 Python 3.10 以上。本项目默认用本机的 Python 3.12：

```bash
cd '/Users/apple/Desktop/科研/Multi Agent/astrion-agent'
./setup_bfcl.sh
```

这个环境是可选旁路依赖，不改变 Astrion Agent 核心的标准库运行方式；`.venv-bfcl/`
和运行结果均被 Git 忽略。

### 2. 配置官方模型凭据

默认 registry/scorer 是 BFCL 官方注册的 `DeepSeek-V3.2-Exp-FC`。官方 handler
固定访问 DeepSeek 官方 endpoint，并读取 `DEEPSEEK_API_KEY`。可以把它写在项目根目录
`/Users/apple/Desktop/科研/Multi Agent/.env`：

```dotenv
DEEPSEEK_API_KEY=你的_DeepSeek_官方_key
```

`python3 -m benchmarks bfcl doctor` 和真实 `run` 会复用 Astrion Agent 已有的标准库
`.env` 加载器；已有 shell 环境变量优先。`--dry-run` 不读取 `.env`。
`.env` 已被 Git 忽略，不要把真实值写入文档、日志或版本库。

`bfcl-eval==2026.3.23` 的 DeepSeek handler 会先初始化 OpenAI 父类，再创建真正的
DeepSeek client；当前 OpenAI SDK 要求父类初始化时已有 key。适配器因此只在官方
BFCL 子进程环境中，把 `DEEPSEEK_API_KEY` 临时复制为 `OPENAI_API_KEY` 兼容别名。
它不会写回 `.env` 或报告，实际请求仍由官方 handler 发往 DeepSeek endpoint。

项目现有的 `AGENT_API_KEY`、`AGENT_BASE_URL` 和 `AGENT_MODEL` 不会自动转发给官方
handler。`bfcl run` 的 API 模型覆盖是显式命令参数，仍由官方 DeepSeek handler
使用 `DEEPSEEK_API_KEY` 请求官方 endpoint。

### 3. 环境检查与 dry-run

```bash
# 检查固定版本、官方模型注册和凭据；不会调用模型
python3 -m benchmarks bfcl doctor

# 只生成本次目录、200 个 ID 和两条官方命令；不调用模型
python3 -m benchmarks bfcl run --dry-run
```

`doctor` 要求版本严格等于 `2026.3.23`，避免数据、prompt 和 scorer 漂移。
适配器兼容该版本 `bfcl version` 在部分安装中的 distribution 名问题，会退回到同一
venv 的 Python 查询 `bfcl-eval` 元数据。

### 3.1 严格对照时的 baseline API 模型

固定版 BFCL 原始将 `DeepSeek-V3.2-Exp-FC` 映射为 `deepseek-chat`。当前默认命令
为了和 Astrion 的 `deepseek-flash` 运行对照，会通过项目内的
`benchmarks/bfcl_official.py` 在当次子进程内把 handler 的 `model_name` 覆盖为
`deepseek-flash`。它不修改 `.venv-bfcl` 中的第三方文件，不改 registry name、
函数 schema、结果目录或官方 scorer。默认温度也从官方原配置的 `0.001`
调为与 Astrion `ChatLLM` 一致的 `0`；仍可用 `--temperature` 显式覆盖。

```bash
# 默认：官方 handler 实际请求 deepseek-flash
python3 -m benchmarks bfcl run --benchmark baseline-200

# 如需复现旧的原始映射
python3 -m benchmarks bfcl run \
  --benchmark baseline-200 \
  --api-model deepseek-chat
```

新报告会同时记录 `registry_model=DeepSeek-V3.2-Exp-FC`、
`api_model=deepseek-flash` 和 `execution_path=official_model_handler_api_override`。
这类结果是“官方 handler + API model 覆盖”的项目内对照，不应冒充
BFCL 官方默认模型配置或排行榜提交。

### 3.2 用 BFCL 测 Astrion 的单轮工具决策

先只检查将执行的 worker、manifest 和官方评分命令，不读 `.env`、不调用模型：

```bash
python3 -m benchmarks bfcl astrion-run \
  --benchmark trial-3 \
  --dry-run
```

确认后运行 3 条真实样例：

```bash
python3 -m benchmarks bfcl astrion-run --benchmark trial-3
```

默认 API 模型依次取 `--api-model`、`AGENT_MODEL`、`deepseek-chat`；服务地址依次取
`--base-url`、`AGENT_BASE_URL`、DeepSeek 默认地址。模型请求使用 `AGENT_API_KEY`，
没有时再尝试 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`。官方 scorer 仍需默认的
`DEEPSEEK_API_KEY` 完成其 handler 初始化。

3 条验证完成后，可以将 `trial-3` 换成 `smoke-20` 或默认的 `baseline-200`。
Astrion v1 刻意不接受 `all-scoring`：其中含 live、多轮、memory 和 web-search 类别，
不能被单轮适配器诚实覆盖。统一报告用 `kind=bfcl_astrion_run` 和
`execution_path=astrion_single_turn_adapter` 与官方 handler 基线明确区分；这类结果默认
是项目内部对比，不冒充 BFCL 官方排行榜提交。
生成期间每条结果都会原子刷新到官方 JSONL，终端因此能显示当前类别组和总进度；
中断时已完成的 result 与 Event 会保留，但 v1 尚未开放 Astrion 断点续跑。

### 4. 运行默认 200 条基准子集

```bash
python3 -m benchmarks bfcl run
```

默认 manifest 位于
`benchmarks/manifests/bfcl_v4_single_turn_baseline_200.json`，包含：

- `simple_python` 40 个；
- `multiple` 40 个；
- `parallel` 40 个；
- `parallel_multiple` 40 个；
- `irrelevance` 40 个。

抽样是固定、可复现的等间隔抽样，而不是每次随机：`simple_python` 每 10 个 ID
取一个，`multiple`、`parallel`、`parallel_multiple` 每 5 个取一个，
`irrelevance` 每 6 个取一个。这样覆盖各类别完整编号范围，同时保持五类等权。

原 20 条 smoke 仍可显式运行：

```bash
python3 -m benchmarks bfcl run \
  --benchmark smoke-20
```

最小真实试跑 manifest 位于
`benchmarks/manifests/bfcl_v4_single_turn_trial_3.json`，分别取
`simple_python_0`、`multiple_0` 和 `irrelevance_0`。运行命令：

```bash
python3 -m benchmarks bfcl run \
  --benchmark trial-3
```

### 4.1 官方全部正式计分类基准

内置短名称 `all-scoring` 对应
`benchmarks/manifests/bfcl_v4_all_scoring_full.json`，直接使用官方 collection，
不枚举或复制 5,217 个 ID：

```bash
# 只核对命令和报告元数据，不读取密钥、不调用模型
python3 -m benchmarks bfcl run --benchmark all-scoring --dry-run

# 正式运行；成本和时间都可能很高
python3 -m benchmarks bfcl run --benchmark all-scoring
```

它覆盖官方 `ALL_SCORING_CATEGORIES` 的 22 个类别，排除非计分的
`format_sensitivity`。官方生成链路涉及 5,217 个条目，其中 memory 类别含 111 个
维持上下文所需的 prerequisite 条目；这些前置条目不作为独立目标计分，所以预计
最终计分 case 为 5,106 个。

该模式的命令语义与 case-id 子集不同：

- generate 使用 `--test-category all_scoring`，不使用 `--run-ids`；
- evaluate 使用相同 collection，且不使用 `--partial-eval`；
- 报告的 `case_count=5217` 表示生成条目数，`expected_scored_cases=5106` 表示预计
  计分目标数；实际完成后仍以官方 score 文件中的总数为准。

内置选择名称共有 `baseline-200`（默认）、`smoke-20`、`trial-3` 和
`all-scoring`。自定义实验仍可使用 `--manifest <路径>`，它与 `--benchmark` 互斥。

### 4.2 实时分组进度

长任务运行时不再静默等待。运行器默认每 5 秒扫描官方 result/score 文件，终端会
显示当前或最近活动类别是第几组/共几组、本组完成数、完整生成组数、总条目百分比
和评分完成组数。类别、完成组、评分组变化或总进度跨过约 1% 时打印；没有变化时
每 60 秒输出一次心跳，避免每个样例都刷一行。

同一状态会原子保存为每次运行目录的 `bfcl_progress.json`，最终阶段会变为
`completed`、`generation_error`、`evaluation_error` 等。统一报告也会在长命令开始
前先以 `preparing` 状态写出，因此中途即可确认 manifest 和命令。刷新间隔可调：

```bash
python3 -m benchmarks bfcl run --benchmark all-scoring \
  --progress-interval 10
```

在另一个终端查看最近运行或指定运行，不会调用模型：

```bash
python3 show_bfcl_score.py --progress
python3 show_bfcl_score.py --progress --run <运行ID>
```

对于优化前已经启动、没有 `bfcl_progress.json` 的旧进程，查看脚本会直接扫描现有
result/score 文件计算当前进度。官方 stdout/stderr 仍完整保存在原有日志中。

### 4.3 中断后的安全续跑

先用只读模式审计指定运行：

```bash
python3 -m benchmarks bfcl resume --run <运行ID> --check
```

它不会加载 `.env`、修改文件或调用模型，会报告已有记录、可复用记录、需重试记录、
剩余生成条目，以及需要修复的 memory 场景和快照。审计拒绝损坏 JSON、重复 ID、
manifest 不一致和仍可能存活的运行进程。

确认后执行真正续跑：

```bash
python3 -m benchmarks bfcl resume --run <运行ID>
```

续跑沿用原报告中的 model、manifest、temperature、线程数和凭据变量名，并在同一个
运行目录再次执行官方 generate/evaluate。它刻意禁止 `--allow-overwrite`：官方
harness 会跳过已有成功 ID，本项目只清除 `result` 以
`Error during inference:` 开头的错误记录。

memory prerequisite 不能只按单条 ID 判断。运行器按每个场景从 step 0 开始核对
“成功 result + 有效 checkpoint”的连续前缀；断点后的结果/checkpoint 会移出工作
状态，`<scenario>_final.json` 恢复为最后一个有效 checkpoint。续跑会重新评分，
因此旧 score 会先移除，避免新旧 attempt 的评分文件混合。所有受影响文件会先复制到：

```text
resume_backups/attempt_NNN/
```

续跑日志写到 `logs/attempt_NNN/`；统一报告保留 `attempts`、`resume_count`、每次
commit、命令、结束状态和本次审计。续跑再次失败时，可以重复执行 `--check`，新的
尝试会使用下一个编号。`--check` 没有费用，真正的 `resume` 会继续模型调用。

每次创建独立的 `benchmark_runs/bfcl/<run-id>/`。其中包括官方要求的
`test_case_ids_to_generate.json`、官方 `result/`、官方 `score/`、命令日志和
`bfcl_run_report.json`。命令不经过 shell 拼接，凭据值不会写入统一报告。

统一报告中的 `official_category_scores` 直接读取官方 score JSON 首行的
`accuracy`、`correct_count` 和 `total_count`；单条对错不由本项目重新判断。
报告还会从官方 result 汇总 input/output/total tokens 和模型响应 latency，并保留
每个结果文件的同类指标；生成与评分命令的完整耗时分别记录在 `commands` 中。
`derived_subset_micro_accuracy` 只是对所选 200 个样例按数量汇总，明确属于
**subset**，不能当作 BFCL 官方 full leaderboard overall。

生成失败时不运行评测；评测失败时仍保留已生成的官方结果和日志。报告的状态可为
`completed`、`dry_run`、`interrupted`、`setup_error`、`generation_error`、
`evaluation_error` 或 `orchestration_error`。

### 5. 查看最近或指定成绩

`show_bfcl_score.py` 只读取报告，不调用模型：

```bash
python3 show_bfcl_score.py                 # 最近一次 completed
python3 show_bfcl_score.py --list          # 所有历史运行
python3 show_bfcl_score.py --run <运行ID>  # 指定运行
python3 show_bfcl_score.py --run <运行ID> --json
```

`--run` 也接受运行目录或 `bfcl_run_report.json` 的完整路径。文本输出包括 subset
总分、正确数、官方分类成绩、token、模型 latency、生成/评分耗时和报告路径；失败
运行会显示状态与错误，不会伪造总分。

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
Astrion Agent commit / manifest / case ids
model / temperature / 线程数
BFCL V4 / bfcl-eval 固定版本
官方 result/score 路径与 SHA-256
```

不要拿 Astrion Agent eval 的分数、BFCL 200 样例 baseline 和官方 full leaderboard 分数
直接比较；它们用的是不同任务、profile 和覆盖率。
