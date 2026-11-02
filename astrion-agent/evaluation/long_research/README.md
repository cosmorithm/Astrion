# 长程科研 Context 对照 v1

以本机 `Agent Test Paper` 25 篇 PDF 的既有封闭资料库为原材料，建立三个单 Agent、每题六篇论文的报告任务：协作组织、通信与辩论、效率与失效。每个任务在第 3、6 次决策前分别注入同样的用户要求；若 Agent 在此前结束，未送达的更新标记为未覆盖，不能当作其违反了未来的指令。不要强迫 Agent 消耗轮数。论文清单和事实要点复用 `evaluation/research`：原件、提取文本及哈希先经 `check_base` 校验，事实草案仍待人工逐条核对 PDF。金标准和评审表不进入模型可访问的项目数据库或任务消息。

每组从同一只读资料库复制独立数据库，仅留该题六篇论文，单 Agent，禁用外部检索和代码执行，两组同样允许本 Agent 历史与证据检索。`legacy` 全程走原有 `Memory.compact`，不会启动 SS 构造器；`ss` 使用 Step 重构及其预算恢复。默认主 Agent 最多 30 轮、字符阈值 16000、本地输入预算 12000 token；压缩模型调用不计入决策轮，模型可以提前完成。两种策略都在本地同一输入预算内发送请求；本地固定序列化的 tokenizer 计数不等同于服务端隐藏模板，输出窗口须另留。旧压缩遇到 token 水位也触发压缩检查，但保留自身五层策略与长度行为。任务更新时序基于各自的第 N 次决策而非相同墙钟时间，可能处于不同研究阶段，应结合轨迹解释。

运行日志、数据库、原始 PDF 和模型响应可能较大且包含私人资料。历史、报告与底层资料保存在所选输出目录；各组结束立即写入单独报告和 `suite.json`，中断后已完成的结果不会被覆盖。`.env` 存密钥，不写入命令行。模型可能曾在预训练中见过公开论文；本测试不是防污染基准。真实调用会产生费用，失败请求和未记录的传输重试可能产生未知用量。

```bash
# 可先单题跑一对；输出目录必须不存在。
.venv-ui/bin/python -m evaluation.long_research --case LR01 --strategy both \
  --output research_data/long_research_eval/my_lr01

# 默认三题、各两组；按题交替执行次序。支持 --steps、--budget-chars、--input-budget-tokens。
.venv-ui/bin/python -m evaluation.long_research \
  --output research_data/long_research_eval/my_suite

# 同一已完成 Step 的真实前缀，两套策略独立回放。源报告可以来自失败后有完整检查点的运行。
.venv-ui/bin/python -m evaluation.long_research.fixed_history \
  --source-report /绝对路径/某个单组报告.json \
  --prefix-steps 10 --steps 12 --output research_data/long_research_eval/my_fixed

# 只在本地准备和核对两个前缀哈希，不调用模型。
.venv-ui/bin/python -m evaluation.long_research.fixed_history \
  --source-report /绝对路径/某个单组报告.json --prepare-only \
  --output research_data/long_research_eval/my_fixed_check

# 若某一组单独重试，比较前强制核对两个报告的前缀哈希。
.venv-ui/bin/python -m evaluation.long_research.fixed_compare \
  --legacy /绝对路径/旧压缩/suite.json --ss /绝对路径/SS/suite.json \
  --output research_data/long_research_eval/my_fixed_comparison.json

# 对若干独立 suite 做汇总；语义通过率保留 null。
.venv-ui/bin/python -m evaluation.long_research.aggregate \
  /绝对路径/suite1.json /绝对路径/suite2.json --output research_data/long_research_eval/combined.json
# 若后一个 suite 是显式重测，可加 --prefer-last；旧尝试仍记录在 JSON。

# 生成独立审查表；核对完整回答、实际引用及原始 PDF，不能把草案当成已确认金标准。
.venv-ui/bin/python -m evaluation.long_research.review /绝对路径/单组报告.json \
  --output research_data/long_research_eval/review.json

# 可选的模型参考裁判：单独调用，默认与被测模型同型号，不视为独立权重或人工结论。
.venv-ui/bin/python -m evaluation.long_research.model_review /绝对路径/单组报告.json \
  --output research_data/long_research_eval/model_reviews
```

固定历史试验只复制截止指定已完成 Step 的原始消息及对应档案、事件和可确认的已读范围，不复制之后的消息、摘要、固定内容或构造结果。原用户消息和已经送达的修改留在前缀中；新的追问只要求继续原报告，不重复泄露被测要求。两个隔离范围的消息、档案记录和工具定义形成 `prefix_sha256`，两组必须匹配。每组仍使用真实模型自行执行工具；两组后续轨迹可以不同。若源运行不足指定 Step，则报告实际截取长度，不得冒称达到长历史。固定回放的输入为相同原始前缀，而不是模型当时已压缩的视图；没有继承源运行的执行器任务计划状态（计划工具调用仍作为历史消息可见），也没有继承固定证据，因此不代表恢复原运行的精确现场。

评分先分开报告：运行状态、实际决策请求、用户更新是否送达、成功且未截断的阅读范围、引用位置检查、重构与历史工具调用、时长和供应商已报告 token。论文的事实与引用语义、人类要求是否满足、完整报告质量均待独立人工复核；引用位置有效不等于结论正确，输出有效不等于任务通过，也不从模型自述或关键词命中直接计算通过率。每题每策略仅一次试运行无法推断总体胜负或统计显著性。用户更新是否在最终输入中、发生了多少压缩、目标句是否能被证据支持，均应结合原始事件手动检查。

模型参考裁判接收临时标准要点、对应 PDF 提取页、完整回答和引用片段；其返回的回答引文必须逐字存在，缺失引用或无效位置不能由模型判为通过，截断会限制结论。该裁判不检查所有用户动态要求；源试题中的部分要点可能超出长任务的明确要求。裁判按覆盖情况可将未提及的要点标为 incorrect，这不等于模型作出了错误断言；表内区分有无回答原句。裁判不能改变正式待人工复核状态；同型号模型可能共享偏差，裁判用量单独计算。

`scheduled_user_updates` 是评测使用的按决策轮注入，不代表工作台已提供运行中的实时追问队列；中断恢复时也不会自动去重。此对照脚本仍只运行单 Agent，以保持两种策略可比；研究模式本身支持主 Agent 和子 Agent 一起使用五层压缩。
