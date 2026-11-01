# 科研续写与并发（2026-11-01）

此前研究入口每次新建 Memory，虽然旧报告保存为产物，再次输入“继续”不会自动继承已读证据与待办。修复后，显式指定 `continue_run_id`，将旧检查点复制到新 scope，再将用户补充要求写入上下文；保留原 run/result/report/checkpoint。界面默认新建研究；选择历史版本后切换为续写，完成后可继续修改新版本。

## 使用与预算

在 `astrion-agent/`，使用已配置的 `.env`：

```bash
# 查看当前项目的历史（每页20条，可加 --offset 20 --limit 20）
.venv-ui/bin/python -m research runs --project 1
# 新任务，默认主50轮 / 每个子50轮 / 子任务累计最多5个
.venv-ui/bin/python -m research run --project 1 "比较项目文献中的方法和局限，附原文引用"
# 使用列表返回的真实ID；每次续写获得新的预算，并另存版本
.venv-ui/bin/python -m research run --project 1 --continue-run research_REPLACE_WITH_RUN_ID \
  "继续阅读未覆盖的文献，核对旧稿的结论，输出完整修订版"
# 小预算或单Agent对照
.venv-ui/bin/python -m research run --project 1 --steps 8 --worker-steps 4 --workers 0 "核对一项结论"
```

工作台：选择项目 → “研究历史”选中一版 → 填写“补充研究要求” → “继续研究”。选择“新建研究”退出续写。运行预算可展开修改；支持主0–50轮（界面1–50）、子1–50轮、子数量0–5。50轮是决策上限，最后一轮仍保留为无工具收尾；不是必须消耗50次，也不限制压缩调用次数。普通核心 CLI/BFCL 默认预算未改变。

## 接口和数据

- `GET /api/research/config`：共享默认预算与上限。
- `GET /api/projects/{id}/runs?offset=0&limit=20`：历史分页、状态、报告产物、续写标记。
- `GET /api/projects/{id}/runs/{run_id}`：单版运行结果。
- `POST /api/research/stream`：原字段不变，新增可选 `continue_run_id`；找不到、越项目或缺少检查点时在开始模型请求之前返回400。格式错误/超预算返回422。
- 新版结果新增 `parent_run_id`、`budgets`、`observed_ranges`；报告 provenance 也保存父版本ID。`steps`、`duration_seconds`、`usage` 均只计本次，不包含历史费用；usage包含主、子决策与压缩。`completed` 仍只表示模型合法调用finish，不等于任务通过独立验收。

原始证据、用户原话、固定引用、压缩视图和 `task_plan` 复制到新上下文。子 Agent 上次的检查点不会作为新子线程恢复，根 Agent 继承上次子报告，按需创建新的子任务。版本引用审计继承历史**实际且未截断的原文阅读范围**，不以历史报告里的引用文字证明来源已读；语义支持仍未自动判定。旧版无 `observed_ranges` 时从旧事件文件提取，不改写旧日志。

每个运行的结果和事件仍位于数据库旁 `runs/<project>/<run_id>/`；上下文在 `context.sqlite3`，报告在产物库。同项目同名报告因产物ID不同而保留多个版本。新结果记录数据库身份，避免同目录的不同测试数据库误取项目同ID的运行；旧结果用项目ID及名称兼容筛选。存储路径移动后需要同步迁移记录中的数据库/上下文路径；当前不提供自动重定位。该项目仍为本机单用户应用，项目作用域不是多人权限系统。

## 并发契约与模块

`tools/research.py` 是委派工具声明的权威位置：

- `run_worker(task, context)`：保留顺序委派兼容入口。
- `run_workers(tasks)`：一次提交1–5项 `{task, context}`，所有任务通过校验且有剩余名额后再启动，等待本批全部结束。与顺序入口共用本次累计上限，失败子任务也占名额，返回顺序与提交顺序一致。
- task最多2000字符，context最多12000字符。批量摘要每个回答最多2500字符，显式 `truncated`；完整子报告用 `report_artifact_id` 交付，`read_project_file` 分页重读。保存失败单独标记，不能假称全文已经交付。

`research/workers.py` 管理有界线程池，每个线程自行创建/关闭Store，各用独立Memory/scope和 `llm.fork(role)` 客户端。真实流式客户端固定自己的角色并独立记录usage；根事件记录加锁串行落盘，SSE队列可接收交错事件。产物配额检查与插入处于同一SQLite写事务，避免并发突破配额。子Agent的工具集中没有任何委派工具。

`research/continuation.py` 校验并复制检查点；`research/runs.py` 管理事件/版本读取；`research/config.py` 集中预算；`research/runner.py` 只负责研究编排、汇总与交付。`research/routes/runs.py` 管理HTTP历史入口，`ResearchHistory.vue` 管理历史交互。`ToolSpec`/`ToolRegistry`、核心调度和默认单次决策保持复用。自定义注入模型若要并发，须实现返回独立实例的 `fork(role)`；没有此接口的旧同步模型仍可用 `run_worker`，批量执行会明确拒绝。

## 固定回归与真实联调

```bash
bash run_tests.sh
bash run_research_tests.sh
.venv-ui/bin/python -B -m unittest tests.research.test_sessions tests.research.test_workers -v
cd frontend && npm run build
```

本地结果：核心219项通过，科研44项中40通过/4项真实Docker测试跳过，API/HTTP6项通过，合计265通过/4跳过；Node18构建通过。新增回归覆盖：5线程屏障证明重叠执行、独立DB/上下文、层级权限、累计上限、单子任务失败、完整报告重读、token去重、并发配额、两次连续续写、旧版本不可变、任务计划恢复、跨项目/数据库拒绝、旧引用不能冒充实际阅读、SSE/API入口。

使用已有真实模型与流式客户端，在独立合成资料库执行“两个子Agent并发读两份资料 → 初稿 → 续写修订版”。两个子任务于05:00:35.043Z/05:00:35.044Z开始，分别于05:00:40.790Z/05:00:40.611Z结束，确认重叠；初稿19.50秒/32,975 token/8次调用，续写12.05秒/8,629 token/1次调用。根/两个子流角色分离，旧结果不变，新报告ID不同；引用位置均找到。初稿为no_tool_call，续写为completed，一个子报告为no_tool_call而非finish。模型保留了合成材料的数字，但超出了期望字数；这不算严格任务质量验收通过，也不证明科研能力提高。

可重放的输入、原始事件和结果保存在本机忽略目录 `research_data/validation/continuation-20261101T050029Z/`，汇总为 `summary.json`。用户最近两次综述的旧检查点只读加载通过，其中一份保留6项待办；没有代用户重跑整套文献。工作台实际验证了历史选择、旧报告展示、续写状态与50/50/5预算。

边界：没有运行中取消、跨进程耐久执行或崩溃副作用重放；并发上限按单次研究计算，尚无跨请求总并发限制。上下文复制会增加磁盘占用；原始数据及历史目前没有自动清理。真实Docker仍不可用，4项跳过不能当作隔离验证。增加轮数和并发不保证结论正确，也不保证更省token。

## 模型请求失败后的恢复

研究运行抛异常时会记录最近一个已完成工具批次的根 Agent 检查点，保留已发生的子 Agent 结果、用量和已读范围。刷新工作台，在“研究历史”中选择失败的运行，输入继续要求即可从该检查点分叉新运行。若失败发生在批次中间，检查点之后的操作可能已经产生副作用但未被记录，恢复时可能重复执行；需要检查事件和已有产物。子 Agent 不会作为线程恢复，已经保存的子报告可从产物库读取。没有有效检查点时不能恢复。

2026-11-01 的运行 `research_263f2210b12e400f843694723ff9c186` 在主 Agent 第13轮模型请求发生 `URLError`；记录不足以确定更底层的原因。其第12轮检查点通过末尾两个工具调用ID与事件日志核对，恢复关联写入该运行目录的 `recovery.json`，原结果和事件未改写。三份子报告已保存为项目产物；恢复会创建新运行并重新计量，不自动重试网络故障。该失败运行耗时约1131秒，记录了2,472,038 token，305次请求；后续预算可在界面调低。

## 输入输出快照与上下文窗口（2026-11-01）

研究日志逐模型调用保存输入消息与工具清单、模型响应（包括服务端返回的 reasoning_content，如有）、工具调用及结果；SSE 活动轨迹和历史事件API `GET /api/projects/{id}/runs/{run_id}/events?offset=0&limit=10` 均可查看，后者按事件分页、limit最多20。旧运行只有输出和统计等既有事件，输入快照缺失时明确显示无法准确还原。`tools/research_history.py` 提供 `list_research_runs` 和 `read_research_event`，限制当前项目、分页输出，旧报告和历史模型响应是待核查数据，不是原始证据。日志作为对应研究历史的持久存储，暂无自动清理；保护和备份应包含 runs 目录。

官方 DeepSeek `deepseek-flash` 的上下文窗口为 1,048,576 tokens；仅对官方端点的已识别模型配置该元数据。研究模式的软字符压缩阈值默认600,000，可在界面或 `--context-budget-chars` 修改；其他提供商仍为12,000且窗口未知。阈值不会精确估计token，不包含token级硬上限，也不保证输出预留或避免服务端超窗；固定证据、工具定义与本轮输出仍占空间。字符数与token数不可互相等同，阈值增大可能增加成本。
