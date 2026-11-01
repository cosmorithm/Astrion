# Astrion Context Eval v1

这是一组专门检查上下文处理的固定合成样例，不是完整科研 Agent 评测。六题分别检查跨轮计算、参数更新、单位换算、记录消歧、证据不足和失败后的正确版本。每题有八个固定历史 Step、题目、只用于赛后判分的答案和关键 Step 编号。`cases_long.json` 保持事实和顺序不变，加入更长的无关过程说明，用于观察历史长度变化；它不是独立样本。样例是人工构造的开发集，数量少、分布人为，不能代表真实科研任务。

每题从同样的固定历史开始比较：`append` 保留完整历史、无需预算目标；`legacy` 调用现有 `Memory.compact`，仍使用字符触发阈值；`ss` 使用严格本地 token 契约的 `ContextConstructor`。三组模型和初始数据相同，区别在上下文处理。

当前实验将 B 设为初始消息的**本地 token 数**乘以 `--budget-factor`（默认 1.25，即初始水位约 80%），目标是 `0.5B`。SS 组按本地 tokenizer 严格检查 0.5B；legacy 的原始压缩参数仍以字符计，但报告同时用相同本地 tokenizer 检查它最终是否在 0.5B 内。追加组不强制预算，因此不是同预算组。旧报告使用字符预算，不能将旧报告中的字符目标与新报告的 token 目标当成同一实验。

默认进行一次无工具回答；`--followups 2` 随后追加两个固定问题与回答，复用初次构建，不进行新的重构或重读。追问用独立的 `followups.json` 及哈希记录；此前 C03 的“溶液质量”有歧义，旧版保留在 `followups_v1_ambiguous.json`，新版明确为“有效成分质量”。多轮模拟有助于衡量构建成本分摊，但不代表真实 Agent 的完整循环、长程重构、自动补读或科研效果。

每次调用的输入、输出、报错、耗时和供应商提供的 usage 都存入结果 JSON；构建阶段与回答阶段的调用按 `purpose` 区分。服务未报告 usage、调用失败或当前客户端输出重试提示时计入未知，不当成零费用。旧运行没有记录底层重试，因此即便成功调用都返回 usage，仍不能确认没有未知的重试费用。字符数不是 token；结果报告中的已知 token 包括摘要、规划和回答。最终答案需为 JSON 对象，`answer` 的字符串或数字与标准答案精确匹配；是否遵守提示词要求的字符串格式另行记录。携带额外单位或解释、格式错误会判错，这可能低估语义正确的自由文本。

SS 组额外报告模型计划选中或近期保护的关键 Step 比例。**选中了 Step 不代表摘要保留了结论，也不代表模型最终答对。** 关键事实的字面线索检查只是需要人工复核的线索；同义表达会产生假阴性，包含字面内容也不能保证逻辑正确。

在 `astrion-agent/` 目录：

```bash
# 只列出样例，不调用模型
.venv-ui/bin/python -m evaluation.context --list

# 用已有 .env 配置的模型运行 3 题 × 3 组，可能需要多次模型调用并产生费用
.venv-ui/bin/python -m evaluation.context --case C01 --case C02 --case C03

# 同一构建后追加两轮追问（增加模型调用成本）。
.venv-ui/bin/python -m evaluation.context --case C01 --case C02 --case C03 --followups 2

# 压力设置：本地 token 目标更紧（同时增加模型调用成本）。
.venv-ui/bin/python -m evaluation.context --case C01 --budget-factor 0.8

# 同一题的加长历史；与短历史仍然是同一题，不能计作独立样本。
.venv-ui/bin/python -m evaluation.context --cases evaluation/context/cases_long.json --case C01

# 对同一个 JSON 结果生成 Markdown 总表（校验样例 SHA-256，拒绝覆盖已有文件）
.venv-ui/bin/python -m evaluation.context.report eval_reports/context_运行标识.json

# 可指定结果路径，避免覆盖已有结果；也可通过 --model / --base-url 临时指定模型。
.venv-ui/bin/python -m evaluation.context --case C01 --output eval_reports/context_trial.json

# 离线测试，不调用模型
.venv-ui/bin/python -m unittest discover -s tests/context_eval -q
```

结果在 `eval_reports/`，被 Git 忽略；JSON 会在每组完成后原子保存。一次运行中请求失败会记录错误和未知用量，并继续后续组；SS 构建失败后如果回答仍然产生，回答基于保留的旧上下文，不能当作 SS 成功。运行前不可覆盖同名结果。请勿将结果直接当作质量提升结论：至少需要更多不同任务、多次独立重复、记录位置与长度变化，以及针对关键事实的人工语义复核。

旧版结果若将 JSON 数字误判为字符串格式错误，可以用 `python -m evaluation.context.rescore 原报告.json --output 新报告.json` 重新判分；加长样例需指定 `--cases evaluation/context/cases_long.json`。该命令保留原始响应和原始评分，不重新调用模型，也不覆盖原报告。

首次真实模型小样本结果与失败原因见 [RESULTS.md](RESULTS.md)。

## 真实 Agent Loop 的历史回查试验

```bash
# 3 个合成历史 × 旧压缩/SS；每组在真实 Agent Loop 中最多 10 轮，调用真实模型
.venv-ui/bin/python -m evaluation.context.live_recall --case L01 --case L02 --case L03

# 先看单题；限制轮数会改变完成率和成本，不等同默认对照。
.venv-ui/bin/python -m evaluation.context.live_recall --case L01 --steps 10
```

试验从 `live_cases.json` 和固定长历史构造早、中、晚三题。起始目标是整理研究过程，不要求统计目标数字；直接调用现有 SS 构造器或 `Memory.compact`，随后提出新问题，由 `agent.run` 和真实的 `search_step_history`、`read_step_history`、证据工具完成回答。所有 Agent 组共享本地输入预算 B；旧压缩原生使用字符阈值，因此其结果未必满足本地 0.5B 目标，报告单独列出，不把它当成同输出预算的严格对照。

评测记录问题提出前答案是否**字面可见**、Agent 调用的历史工具、工具结果是否包含答案、最终回答与成本。报告检查答案最早是在初始视图、后续决策视图还是历史工具结果中出现；只有答案最早出现在工具结果且最终答对，才标记为“工具先返回且答对”。即使如此也不是因果证明或事实语义裁判。历史是合成数据，不能代表真实科研记忆。提问阶段实验性放宽字符水位，token 水位仍可触发后续重构，因此不是工作台默认配置的原样行为。具体预算、构造过程、事件快照、答案和错误保存在 `eval_reports/`，可能包含模型的详细输出，应按研究数据保存。

旧版首次运行 `context_live_recall_v1.json` 没有保存 Agent 阶段的重试诊断，也未保存临时 SQLite 中的完整证据档案；已知费用仅是下限。后来脚本记录 Agent 重试提示并把证据档案的内容快照纳入结果。两者的执行代码不同，应以报告的运行标识和源码为准，避免把后续改进追溯为首次运行结果。
