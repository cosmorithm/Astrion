# P0：工具协议文本被当作科研报告发布

## 实际事故

2026-11-01 12:25:32（Asia/Shanghai），运行 `research_0d763631c73d41539b6a8bbcbdf930fb` 对“请结合所有资料，介绍一下multi agent的优劣，形成综述”结束为 `no_tool_call`，但最终 content 是 DSML 格式的 `read_evidence` 调用。该内容没有执行，却被保存为 research-report.md，工作台显示“已返回回答”。这属于错误交付，不是有效综述。

该次运行 42.9 秒，10 轮决策及3次摘要，输入59,729、输出3,697、合计63,426 token。14次工具提议包含7次 read_evidence、3次 list_documents、2次 search_knowledge、1次 list_artifacts 和1次 update_task_plan；计划状态枚举及目录 limit=50 各发生一次错误。没有调用 read_document / read_knowledge，没有启用子 Agent。

原始事件、结果和错误报告保留，没有删除或把旧失败改写成成功。它们含项目资料，继续留在本地忽略目录。脱敏控制文本已固定为 fixtures/regressions/protocol_as_report.json。

## 原因与触发条件

1. 最后一轮 tools=[]，但之前仍发送原生 assistant.tool_calls/tool 历史。本次模型服务把继续调用工具的意图输出到普通 content；这种请求形态可能促发继续调用，但不能仅凭一个案例归因于供应商内部实现。
2. 核心循环用“没有原生 tool_calls + content 非空”直接认定存在回答，没有识别泄漏的控制协议。
3. 研究层只检查 answer 非空就保存报告；流式层把 content 原样送往展示通道。三处共同把无效输出变成了产物。
4. 19份资料的长目录超过单工具预算，被第一层立即外置；模型之后花步数重读目录/检索档案，最后再列目录又被外置。这是耗尽预算的贡献因素，不是协议误发布的充分解释。

## 修复边界

- response_contract.py 统一答案校验和流式保护。观察到的 DSML 控制标记在普通答案中被判为 `tool_protocol_in_answer`，主循环返回 `status=error, answer=null`；原始内容仍保存在事件/上下文供排查。正常提到工具名称，以及代码块/行内代码中的协议示例不作为失败。
- 原生 finish.answer 与终止工具返回值也经过校验。文本不会被解析成可执行调用；调用权限仍只来自结构化 tool_calls 和 ToolRegistry。
- 流式检测跨任意分块保留可能的标记前缀；发现协议时停止展示后续协议文本。原始消息继续组装供核心校验；正常内容仍逐块流式输出。
- 最后一轮使用只读历史快照：保留用户原话及证据，把原生工具消息转为数据，不改动原始日志、不加额外模型调用或步数。正常直接回答仍是 no_tool_call。
- 研究报告保存前增加防御校验，前端将错误显示为未完成，清除暂定过程文本，且不提供不存在的报告下载。
- list_documents 保持字段兼容，按约2600字符分页，并返回 total / next_offset；limit 仍为1–20。不会为了缩短目录删掉资料。文档过多时须继续翻页。

## 验证

- 核心219项通过（增加9项协议/流式/终止/最终请求/报告回放测试）。
- 科研扩展35项中31通过，4项真实 Docker 测试因环境缺失跳过；新增长目录逐页完整及压缩后可见性回归。
- API/HTTP 5项通过，包含模拟上游分块输出原失败协议，经真实 StreamingChatLLM、Agent、SSE 和产物层，验证 error 状态、无协议展示增量、无报告产物。
- 前端构建通过。以上共255项通过、4项跳过，不包含真实模型质量分数。
- 使用原运行的既有证据，对修复后的最终请求执行一次真实 deepseek-flash 流式验证：23.8秒、7,712 token（输入4,181 / 输出3,531），返回2,803字符正常综述，没有原生调用或 DSML。仅验证收尾格式与调用链，未重跑原任务，也未独立验证全部结论。记录为 research_data/incidents/p0-finalization-live.json。

复现：在 astrion-agent 执行 `python3 -B -m unittest test_response_contract`；完整回归运行 `bash run_tests.sh` 与 `bash run_research_tests.sh`。

仍需独立处理多文献覆盖、语义支持、计划枚举错误、非目录证据过早外置和摘要失真。此修复不保证模型以后永不生成异常协议；它保证已识别的这类输出不能再被发布为正常报告。测试通过不等于完整文献综述任务质量达标。
