# Astrion 科研能力：分阶段实现与复现

本次交付从现有 ReAct、ToolSpec / ToolRegistry、工具注入、五层上下文、证据归档和 Vue / FastAPI 扩展。默认仍为 `single` 决策；不默认启用多次模型复核。固定任务在 `fixtures/research/cases.json`，示例资料为合成验收材料，不作为真实科学证据。

## 安装与原有数据

在 `astrion-agent/` 执行：

```bash
.venv-ui/bin/python -m pip install -r requirements-research-test.txt
bash run_tests.sh
.venv-ui/bin/python -B -m unittest discover -s tests -v
```

前端要求 Node 18+；本机已有 Node 位于 `/Users/apple/.nvm/versions/node/v18.20.7/bin`。原有 `python3 agent.py`、`python3 -m research`、旧资料 API、工具名称和行号引用继续可用。PDF / OCR / 语义检索是研究环境的可选依赖，不强加给标准库核心 CLI。

SQLite 采用加列、加表迁移，保留已有 document ID、原文和记忆；`documents.metadata` / `original` 保存位置与原始输入，旧数据缺少这些字段时按原有文本阅读。迁移前建议备份现有数据库；应用不会删除旧资料或重写其文本。

## 第一阶段：把资料读全

已接入主 Agent、子 Agent 和工作台：`list_documents`、`document_outline`、`search_knowledge`、`read_knowledge`、`read_document`。已有任务计划声明接入研究工具装配，状态更新仍由核心循环负责。子 Agent 继续不能再次委派。

```bash
.venv-ui/bin/python -m research project-add paper-study
# 将下面的 1 换成返回的项目 ID
.venv-ui/bin/python -m research document-add --project 1 fixtures/research/rag_en.md
.venv-ui/bin/python -m research document-add --project 1 fixtures/research/agents_zh.md
.venv-ui/bin/python -m research document-add --project 1 fixtures/research/reading_fixture.pdf
.venv-ui/bin/python -m research documents --project 1
.venv-ui/bin/python -m research outline --project 1 3
.venv-ui/bin/python -m research read --project 1 3 --page 1
.venv-ui/bin/python -m research read --project 1 3 --page 1 --offset 1000
```

PDF 限制为 20 MB / 300 页；TXT/MD 仍为 1 MB。每页保留稳定页号、提取方法、全局行号、扫描嫌疑和警告；有 PDF 目录时使用目录，否则以标题规则识别章节并标记推断来源。原始 PDF 字节保存在资料库，可供复核。没有文本的页面明确插入缺失标记。

提取不保证多栏、表格、公式或阅读顺序完整，统一携带 `layout_unverified` 和 `tables_and_equations_may_be_incomplete`；这不是自动识别所有丢失公式的能力。可用 `document-add --ocr` 或工作台复选框启用 Tesseract OCR（需安装 `tesseract` 及 `eng` / `chi_sim` 语言包），逐扫描页处理并标记 OCR 未验证。OCR 失败保留页面和错误状态，不冒充完整原文。

`read_document` 支持 page / section / 行范围及字符分页，按原选择和 `next_offset` 接续，长单行也能读全。`read_knowledge` 保持 80 行 / 6000 字符上限，新增接续信息。返回页号、章节、来源及质量信息进入证据归档；原有 `[D1:L1-L10]` 引用仍有效，部分行不冒充完整已读范围。

### 关键词与跨语言语义检索

关键词通道使用词项匹配、精确短语加分；多语言通道使用 FastEmbed 本地 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`。按两通道 RRF（k=60、等权）融合，以文档 ID / 行位置稳定打破平局，并按原文哈希与位置去重。小片段向量按文档版本、后端和内容缓存进 SQLite；支持 offset / next_offset 分页。

```bash
export ASTRION_EMBEDDING_BACKEND=fastembed
export ASTRION_MODEL_CACHE="$PWD/research_data/models"
.venv-ui/bin/python -m research search --project 1 '如何利用检索到的文献生成答案' --mode hybrid
.venv-ui/bin/python -m research.acceptance --stage 1 --live-model --steps 7 --output research_data/stage1-acceptance.json
```

本地编码器首次加载下载模型，不使用模型 API key。未配置时仍支持关键词检索，并明确返回 `semantic=not_configured`；加载失败返回 `unavailable` 与降级结果，不能把降级当作已验证跨语言语义效果。`--mode semantic` 无语义后端时不会冒充语义召回。

工作台资料页支持 PDF 上传、可选 OCR、目录和页码浏览、接续读取与检索分页。结果展示提取警告与语义通道状态。

### 模块关系

```text
tooling.py                  唯一 ToolSpec / ToolCall / ToolResult / ToolRegistry 契约
tools/builtin.py            基础文件、算术与控制工具；agent.py 保留兼容导出
tools/knowledge.py          资料工具声明与绑定，按 Agent 分别统计已读范围
tools/research.py           角色工具装配；主 Agent 额外获得 run_worker
agent.py                   循环、状态、调用验证、事件和上下文调度
research/runner.py         科研编排、主子 Agent 生命周期
research/prompts.py         研究与收尾提示
research/citations.py       引用位置检查（不等于语义支持）
research/store.py           兼容资料与记忆入口
research/storage/          持久化、迁移、位置与分页
research/ingestion/         TXT/MD/PDF 与 OCR 适配
research/retrieval/         关键词、向量适配与融合排序
research/routes/           HTTP 校验、项目边界和响应转换
research/runs.py           持久化每次研究的 Event v1 与结果
frontend/src/components/   独立资料交互，主界面负责组合
```

业务工具不导入 `agent.py`；核心循环不导入科研存储。新工具先在对应能力模块添加 ToolSpec 与实现，再在 `tools/research.py` 装配适用角色；结构化证据需在 `memory_evidence.py` 添加明确适配。契约不在 CLI/API 重新定义。API 和前端只调用业务入口。

每次研究在资料数据库旁的 `runs/<project_id>/<run_id>/` 保存 `events.jsonl` 与 `result.json`。最终结果分别提供结束状态、引用位置检查、耗时和模型 usage。语义支持仍需独立的逐结论原文核查；不以 `locations_found` 或模拟模型测试冒充研究质量提升。

### 第一阶段实测记录（2026-10-31）

- 原有离线 210 项、新资料行为 7 项、新资料 API 1 项、原有 API/HTTP 4 项通过；Node 18 前端生产构建通过。
- 真实多语言编码器：中文问题召回英文 PDF 方法页，语义通道为 available；此小样例不能代表全面检索效果。
- 真实 Tesseract：扫描样例提取到 128 和 81，并保留 OCR 未验证提示。三页 PDF 已渲染目视核对。
- `stage1-acceptance.json`：真实模型主动完成，6 轮决策，总计 7 次调用（含摘要），37.7 秒、29,447 token。跨资料引用位置可核对；人工抽查 RAG 定义、协作风险及扫描缺失描述得到来源支持。回答开头对中英文主题覆盖的概括偏强，后文有所限定；未给出全量语义通过率，也未声称比旧版本质量提高。
- 仍有限制：章节识别依赖 PDF 目录或标题启发式，表格/公式不保证完整；语义模型需配置并预下载；不对任意资料自动证明语义支持。
