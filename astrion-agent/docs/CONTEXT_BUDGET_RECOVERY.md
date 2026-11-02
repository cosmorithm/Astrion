# 上下文预算恢复

本地输入预算 `B` 限制消息和工具定义的固定 JSON 序列化 token 数，不是服务端隐藏模板的保证。输出空间和服务端安全余量应在设置 B 时另行预留。任何模型请求发送前仍会按 B 检查；无法容纳不可缩减的系统规则、用户输入、工具协议等内容时，明确失败并保留原档案。

进入 80% 字符或本地 token 水位后，只要有至少一个完整 Step 即可重构。默认优先 0.5B；保护内容超过该目标时，优先保留完整近期观察并允许目标到 0.8B。仍不够时，按时间顺序将部分大型工具结果替换为带可变长度预览、明确截断标记和档案引用的回执；在有空间时尽可能增加该结果的预览长度。调用和结果的 ID 配对、用户输入、原始事件及原始结果不变。必要时目标可以提高至 B，增长空间相应减少。工具返回的原文依旧可通过 `read_evidence` 分页访问；预览并不保证保留对任务最重要的片段，模型需要时必须重读。后续重构保持活跃的回执，不会悄悄扩回正文；明确选择旧 Step 的 RAW 仍可取出原文。

仅有近期 Step 而无需选择旧历史时，不调用 LLM 生成摘要或规划；存在旧 Step 时仍按原策略生成或复用 SS、规划和逐项检查。记录本次目标、受保护部分大小、回执数、各个回执来源和实际本地 token；重构失败事件包含真实错误，不再沿用上一次成功的状态。`0.5B` 是优先目标，不是每次都能达到的硬条件。

## 复现

从 `astrion-agent/` 运行：

```bash
# 不调用模型：仅重放已保存的旧报告的最后一个工具批次。
.venv-ui/bin/python -m evaluation.context.replay_failed_batches \
  --report eval_reports/context_recall_reasoning_v2_l03.json

# 调用真实模型；仅用人工构造的数据，需已配置密钥及本地 tokenizer，会产生费用。
.venv-ui/bin/python -m evaluation.context.live_budget_pressure \
  --output eval_reports/my_budget_pressure.json

# 独立复测 L03，默认 10 轮。输出文件不得已存在，会产生费用。
.venv-ui/bin/python -m evaluation.context.live_recall \
  --case L03 --strategy ss --steps 10 --output eval_reports/my_l03_ss.json
```

离线回放只提取旧报告中最后一批实际工具调用，加用户输入及一个合成的旧 Step，用于测试预算恢复和原文可读性；它不是整个旧任务的重放，也不验证答案质量。`eval_reports/` 不随 Git 推送，旧报告需在本机存在。真实压力测试由模型自主选择是否按要求一次调用五个工具，因此单次运行是否触发压缩并非保证；请检查生成的 trace。用量统计基于服务端报告，失败或重试请求可能另有未知用量。
