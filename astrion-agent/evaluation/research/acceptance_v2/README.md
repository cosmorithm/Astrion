# Astrion Research Acceptance v2

这组样例用于人工检查研究任务中的结论和证据：11 道题、20 个检查点、13 篇原始论文。`cases.json` 逐条记录预期答案、对应论文的版本哈希、PDF 页码、提取文本行号，以及可能超出证据的推断。原件页面已由助手渲染并目视检查；**独立人工复核尚未完成，因此目前不是人工金标准，也不能据此宣称通过率。**

R01/R03 为跨论文比较；R07/R08 为事实与分类；R11/R12/R13 为数字、单位与条件；R15/R17 为证据不足；R20 为基于前次报告续写；R21 为运行中需求修改。数据源与旧题库重叠，案例已用于开发，不能当作从未见过的独立测试集。

## 校验资料

以下命令从 `astrion-agent` 目录执行，默认使用 `research_data/research_eval_v1/corpus.sqlite3` 和 `/Users/apple/Desktop/科研/Agent Test Paper`。异地运行需准备相同版本的原件和数据库，并用 `--source`、`--base` 指定路径。缺少基库时，参考上一级 Research Eval v1 文档的 `prepare` 流程；不要覆盖已有数据库。

```bash
.venv-ui/bin/python -m evaluation.research.acceptance_v2 validate \
  --source '/Users/apple/Desktop/科研/Agent Test Paper' \
  --base research_data/research_eval_v1/corpus.sqlite3
```

该命令校验原件及数据库中的原始字节、提取文本的哈希、数据库文档 ID、页码与行号范围、题号与检查点 ID；成功只说明资料版本和位置匹配，**不证明论文原文支持预期结论，更不证明 Agent 答对**。行号基于指定提取版本，PDF 表格和公式须以原始页面为准。

## 运行与对照

以下运行会使用当前配置的真实模型并产生费用；每次使用新数据库副本，不将检查点或预期答案提供给受测 Agent。默认单 Agent、最多 10 轮；运行结果目录由 `--output` 指定。需先配置项目已有的模型密钥。

```bash
# 新工具集：包含结论—证据登记工具
.venv-ui/bin/python -m evaluation.research.acceptance_v2 run-case \
  --case R07 --toolset claims --mode single --steps 10 \
  --output research_data/research_eval_v1/acceptance_v2

# 旧工具集：保持旧评测的工具白名单，供对照
.venv-ui/bin/python -m evaluation.research.acceptance_v2 run-case \
  --case R07 --toolset baseline --mode single --steps 10 \
  --output research_data/research_eval_v1/acceptance_v2
```

`--case` 可选 R01、R03、R07、R08、R11、R12、R13、R15、R17、R20、R21。前 10 题复用旧研究评测运行器与问题，运行前检查题目和文档列表是否与当前 v2 一致；结果同时保存原评测报告和带 v2 检查点信息的 `acceptance-reports`。多 Agent 可使用 `--mode multi --workers 3 --worker-steps 6`。报告会记录工具清单、预算、模型、代码版本、用时和 token；不同工具集必须分别标记，不能当成同一实验条件。

`claims` 工具集在旧白名单上增加 `record_claim` 和 `list_claims`。登记关系只是记录，不能视为结论受到原文支持。旧报告及旧评测命令的默认工具集不变。v2 检查点与旧题库原有 key points 不同，旧版人工复核模板不能替代 v2 的逐条复核。

R20 先运行第一阶段，再在第一阶段有答复时从该 run 续写；如首阶段无答复，后续阶段不会发生，不能把它计为续写能力的成功或失败。报告中的 `continuation_stages`、两个 run ID 和状态应一并检查。

```bash
# R21：第 3 个决策步骤开始前注入新需求，目前仅支持单 Agent
.venv-ui/bin/python -m evaluation.research.acceptance_v2 run-case \
  --case R21 --toolset claims --mode single --steps 10 \
  --output research_data/research_eval_v1/acceptance_v2
```

R21 保存调度及实际送达的步骤；如果 Agent 在第 3 步前结束，`all_delivered` 为 false，未送达的未来需求不能算其违反。必须先确认更新已送达，再审阅最终答案对新旧需求的处理。R21 报告不自动判定语义，执行异常需查看相应 run 目录和日志，不可算作通过。

## 逐条人工复核

1. 人工审阅者先根据 `cases.json` 中的 PDF 文件版本与页码打开原页，再参照提取行号；确认预期要点和「超出证据」描述本身准确、条件完整。
2. 对运行报告中的每条重要结论，分别记录引用是否可定位、原文是否支持该结论；后者可以填写支持、部分支持、不支持或无法判断，并写明理由和具体引用。定位成功并不等于语义正确。
3. 保留审阅者、日期、使用的资料版本、报告路径、每个检查点的判断及分歧。遇到不清晰的原件或提取错误，保留待定，不强行判通过。

在独立人工复核标准与输出之前，所有语义结论和总通过率都应保持待定。独立评审与模型初审可以配合，但模型判断不能代替人工签署。

这里将答案内容与引用质量分开复核，借鉴 [ALCE 对回答正确性与引用质量分别评价](https://aclanthology.org/2023.emnlp-main.398/) 的思路；本实现不是 ALCE 官方评测器，也不声称复现其评分。
