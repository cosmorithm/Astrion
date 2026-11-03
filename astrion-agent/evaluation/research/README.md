# Astrion Research Eval v1

这是一套基于用户本地 `Agent Test Paper` 中 **25 篇原始 PDF** 的封闭资料科研考试。试题 [QUESTIONS.md](QUESTIONS.md)，标准要点草案 [ANSWER_KEY.md](ANSWER_KEY.md)，论文版本清单 [CORPUS.md](CORPUS.md)。权威数据是 `corpus.json` / `cases.json`，Markdown 由 `documents.py` 生成；不要把答案草案发送给受测 Agent。

## 数据与诚实边界

20 题、40 个要点，涵盖 6 题三论文比较、4 题单篇事实、4 题数字和条件、3 题限指定页的证据不足、3 题跨页阅读（其中 R20 含续写）。每篇论文至少出现在一题；开发集 10 题涉及 13 篇，保留集 10 题涉及其余 12 篇，两边论文不重叠。每个受测实例仅导入本 split 的论文，不能调用网页、外部检索、代码执行或历史研究工具；标准答案与其他 split 论文均不进入受测数据库。与不使用工具的纯模型并非同一实验，这里比较同工具条件下是否允许创建子 Agent。

标准要点由本次实施根据原始 PDF 前两页的可提取文本拟定，锚点已程序核验，P05、P13、P25 首页还做了渲染检查；**尚未全部由独立人工逐条复核，属于草案而非确认金标准**。3 题证据不足仅针对题目明确限定的 PDF 页，不能泛化为整篇没有答案。大多数题目的证据在前两页；这不是完整论文深度、全部 19 篇综述或真实科学正确性的充分基准。题目无人工生成的严格数值答案 JSON 输出契约；不以字符串命中充当事实判定。模型可能预训练时见过论文；分集不是防止模型预训练污染。

提取器对 P13 报告字体映射异常、P15 报告缺少 fontTools、P22 报告 XObject 调用上限而可能跳过内容；资料库元数据附警告，相关事实须回看 PDF。所有 PDF 的表格/公式及版面默认未验证。语义裁判会收到所引文和论文页面（超过 12000 字符会标截断），引用可能同样受提取误差影响。对实验数字不能仅凭提取文本宣称已完整核验。

## 使用

在 `astrion-agent/` 目录运行，安装现有研究依赖，并在项目 `.env` 中配置模型；不需要把 API key 放进命令或报告。

```bash
# 先检查源文件版本、25 篇覆盖与各题 PDF 页面锚点（不调用模型）
.venv-ui/bin/python -m evaluation.research validate
.venv-ui/bin/python -m evaluation.research list --split dev

# 仅首次或使用一个新的目标路径时执行；不会覆盖已有数据库。
.venv-ui/bin/python -m evaluation.research prepare \
  --source '/Users/apple/Desktop/科研/Agent Test Paper' \
  --base research_data/research_eval_v1/corpus.sqlite3

# 单题对照。两组使用同样模型、资料和主轮数；多 Agent 可自行决定是否委派。
.venv-ui/bin/python -m evaluation.research run --case R07 --mode both \
  --steps 10 --workers 3 --worker-steps 6 --budget-chars 12000

# 一次指定两题；或使用 --split dev / --split holdout / --split all。
.venv-ui/bin/python -m evaluation.research run --case R01 --case R07 --mode both
```

科研考试默认主 Agent 最多 10 轮、每个子 Agent 最多 6 轮、多 Agent 组累计最多 3 个子 Agent；轮数可以显式调整，子 Agent 数可以在 0–3 之间设置。单 Agent 组不创建子 Agent，工作台的研究预算不受此设置影响。

`--split all` 的 `both` 会产生 40 次研究运行；R20 是两段续写，每组两次研究。命令会调用真实模型，可能花费大量 token。轮数、子 Agent 数和字符预算均是上限/触发阈值，不是总 token 或费用硬上限；没有任务时间上限。建议先运行单题观察实际费用。每次评测使用独立数据库副本（目前约 112 MB，删除不属于 split 的文献后 SQLite 不一定回收空间），全量可能占用数 GB；报告和轨迹可能含论文文本。

第一次实施已建好默认资料库 `research_data/research_eval_v1/corpus.sqlite3`，请勿对它直接写入项目记忆或产物。每次运行复制一份，核对 25 篇原件、数据库内原文哈希与提取文本哈希；删除副本中其他 split 文献，运行完保留副本、上下文与运行事件便于复盘，不接触工作台现有数据库和项目。

## 裁判与复核

程序裁判检查是否产出有效回答，以及引用是否在工具返回的原文范围。目录、未返回原文的元数据和截断片段不算正文；工具返回也不能证明模型之后完整看到正文。没有行号引用或运行未完成仍会判失败。行号未被覆盖时改为 `pending`：程序正则也会识别“不能引用 [D1:L1-L10]”这样的示例，不能自动认定它是正式引用。人工复核确认后可判定；位置有效也不意味着事实正确。语义、金标准和无依据的结论默认待复核，即使模型调用了 finish 也不会自动给通过。

这是评分语义变更，旧报告保留原判，不与新评分直接混合计算通过率。一次真实小样本的证据核对和评分变化见 [证据链复核](EVIDENCE_REVIEW.md)。

```bash
# 可选：单次独立模型参考裁判，阅读评分要点、指定页及 Agent 实际引用；产生额外费用。
.venv-ui/bin/python -m evaluation.research model-review /绝对路径/单题报告.json \
  --judge-model deepseek-flash

# 如使用其他裁判服务：设置 ASTRION_JUDGE_API_KEY 和 ASTRION_JUDGE_BASE_URL，
# 或传 --judge-base-url。报告与 Agent 费用单独记录；同模型裁判并非独立模型权重。

# 导出人工复核模板，核查答案、原始 PDF 和金标准后填写 reviewer、gold_reviewed、
# citation_support/reason、unsupported_claims/reason、每项 point 的 status/reason。
.venv-ui/bin/python -m evaluation.research review-template /绝对路径/单题报告.json --output /tmp/review.json
.venv-ui/bin/python -m evaluation.research apply-review /绝对路径/单题报告.json /tmp/review.json

# 只传每个原始运行的一个版本；不要同时传原始报告与它的复核副本。
.venv-ui/bin/python -m evaluation.research summary /绝对路径/报告1.json /绝对路径/报告2.json
```

模型裁判必须返回每个要点的结构化判断及可在答案中逐字找到的引文；无效 JSON、缺要点、编造回答引文、页截断均不会产生自动通过。模型裁判只是参考分，不改变正式 case outcome；输出仍需人工复核。复核不覆盖原报告；所有已判定要点、引用支持、无无依据事实和原始程序检查均通过，才能标为通过。`unknown`/未核对金标准不能变成通过。汇总区分已知失败、语义待审和金标准未审，金标准未审时通过率为 `null`，并记录原始报告位置、耗时、Agent token 和模型裁判 token（裁判调用与 Agent 用量分开）。

## 本次实施：小规模真实链路试验

使用已配置的 `deepseek-flash`，选择 R01（3篇对比）、R07（单篇结构与规模），单 Agent 和多 Agent 各一次；每次主 Agent 4 轮，多 Agent 最多 1 个子 Agent、子 Agent 3 轮，上下文字符阈值 12000。**这是调试用极小预算，不是正式基准成绩，也不代表 50 轮的能力。**

| 题目 | 组 | Agent 状态 | 引用位置未通过/引用总数 | 子 Agent 实际创建 | Agent token | 耗时 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| R01 | 单 | no_tool_call | 18/18 | 0 | 29,178 | 54.0s |
| R01 | 多 | no_tool_call | 29/29 | 0 | 19,615 | 26.6s |
| R07 | 单 | no_tool_call | 4/4 | 0 | 17,407 | 20.9s |
| R07 | 多 | no_tool_call | 8/8 | 0 | 17,252 | 19.5s |

两题多 Agent 组都没有实际委派，所以无法用这 4 次运行评估子 Agent 的收益。部分回答明确说明只阅读了目录或未读足正文，不能把目录给出的行号视为已经阅读原文；这些引用没有通过位置检查，但不能据此推断每一个结论都错误。全部标准答案与语义仍待人工复核，不发布通过率或宣称科学结论质量。模型参考裁判对 R07 单 Agent 运行完成 1 次调用（3650 token，`deepseek-flash`，与受测模型同型号），参考结果为 fail；仅供调试，不能代替独立人工结论。

本地结果在 `research_data/research_eval_v1/pilot-20261101/`。`reports/` 是四份不可覆盖单题结果，`runs/` 包括每次独立数据库、上下文、事件和产物，`model-reviews-v2/` 是参考裁判报告，`summaries/` 的最新版区分金标未审/语义待审并将通过率留空。早期调试汇总未区分金标准未审，不能当作正式通过率。试验期间改进了报告元数据和论文提取警告，早期四份原始报告不含后来新增的 base 文件哈希和代码版本；保留原样，不追认这些字段。此后新报告会记录数据库哈希、git HEAD、工作树是否有修改和 Python 版本；`worktree_dirty=true` 时不能仅凭 commit 重现。

重复运行相同题目应保存独立报告；可在一致的预算和数据版本下重复若干次、记录方差。两组所允许的子 Agent 数不同、实际调用成本可能相差很大；没有等额 token 硬预算，因此本版本不能宣称同成本比较。

公开科研测试集接入和 QASPER 复现说明见 [public_research/README.md](../public_research/README.md)。
