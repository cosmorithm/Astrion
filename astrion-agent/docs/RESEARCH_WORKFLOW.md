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

## 第二阶段：获取外部证据

主 / 子 Agent 和工作台“外部证据”页均接入 `search_literature`、`doi_metadata`、`read_webpage`。文献服务适配器为 Crossref，与模型服务独立；默认公共接口无需 key，可设置 `ASTRION_LITERATURE_BASE_URL` 和 `ASTRION_LITERATURE_EMAIL`。核心循环不处理 Crossref 字段。

```bash
.venv-ui/bin/python -m research literature --project 1 'retrieval augmented generation'
# 把返回的 next_cursor 原样传入 --cursor，不能自行拼造
.venv-ui/bin/python -m research doi --project 1 10.1038/nature14539
.venv-ui/bin/python -m research doi --project 1 10.1038/nature14539 --offset 20
.venv-ui/bin/python -m research source-add --project 1 https://arxiv.org/pdf/2005.11401
.venv-ui/bin/python -m research source-add --project 1 https://arxiv.org/abs/2312.10997
.venv-ui/bin/python -m research.acceptance --stage 2 --live-model --steps 7 --output research_data/stage2-acceptance.json
```

搜索返回标题、作者、年份、DOI、来源、出版物类型和可能存在的摘要/全文链接。显式按 relevance score 排序；页内按 DOI 去重，后续筛选仍需模型核对相关性。Crossref 并非全部文献目录，某些 arXiv 的 DataCite DOI 不在其中；缺失或访问失败明确返回，不假造元数据。

DOI 接口返回参考文献元数据分页；`metadata`、`paper_abstract`、`web_page` 和 `pdf_text` 区分来源类型。arXiv `/abs/` 页标为摘要页，普通网页仅称页面正文；下载 PDF 也不等于模型已读全部页。模型必须使用 `read_document` 接续，并保留提取不完整的警告。搜索元数据和摘要不作为已读论文全文。

获取内容先存入 SQLite 的 `source_snapshots`（原始字节、URL、访问时间、SHA-256），再进入统一 documents；同 URL / 同内容重访复用文档 ID，但增加访问快照；内容变化生成新版本。检索、行号、页码与证据归档继续使用第一阶段接口。

网络层独立在 `research/providers/http.py`：只允许公共 HTTP(S) 80/443，固定经校验的 DNS 地址并保留 TLS 主机名校验，重定向逐次重新验证且跨来源不传凭据，最多 5 跳；读取大小最多 20 MB。网页请求 15 秒、元数据请求 25 秒；限流只在 Retry-After 不超过 1 秒时额外重试一次，其余交还结构化错误。错误区分访问拒绝、DNS、网络、超时、限流、过大内容、提取失败。不会把失败页面当成已读全文，也不绕过登录或付费墙。

新增模块：`research/providers/{http,literature,web}.py` 负责外部适配，`research/storage/sources.py` 负责快照，`tools/external.py` 负责声明和绑定，`research/routes/external.py` 负责 API，`ExternalSources.vue` 负责交互。新服务只需实现同样的 search / metadata 或 fetch 接口并在装配入口替换。

参考契约：[Crossref REST API](https://github.com/CrossRef/rest-api-doc)、[pypdf 提取边界](https://pypdf.readthedocs.io/en/6.21.1/user/extract-text.html)、[FastEmbed 多语言模型](https://qdrant.github.io/fastembed/examples/Supported_Models/)。

### 第二阶段验证与限制（2026-11-01）

- 外部服务/错误/快照/API 回归 7 项通过；此前资料与原有接口回归一并通过，前端构建通过。
- 真实 Crossref 搜索暴露了默认游标顺序的问题，已显式添加 score 降序排序；真实 DOI、公开 arXiv PDF 和摘要页可获取。网络验证也实际遇到过 DNS 失败和请求超时，均得到明确错误。
- arXiv DOI 通常由 DataCite 注册：已补 `research/providers/doi.py`，仅在 Crossref 返回 404 时回退 DataCite；不把超时、限流或权限失败吞掉。
- 第一次阶段验收耗时 87.1 秒，模型 7 轮决策、3 次摘要，共 49,992 token，预算收尾为 `no_tool_call`。读取了 RAG 原论文 PDF 开头和另一篇综述的摘要页，生成限定为摘要级的比较；没有读全两篇论文，不能标为完整全文研究完成。该运行发生在 DataCite 回退及摘要页专门分类补丁之前。
- 手工核查发现回答的图 1 说明引用范围过窄，位置检查仍可通过。因此本次证据支持只算部分核查，语义裁判和来源绑定问题仍按根目录 PROBLEMS.md 跟踪，不宣称已全部解决。
- Crossref 查询会返回书籍、预印本等多种记录，并可能存在同题不同 DOI；当前按 DOI 去重，尚未自动判定不同版本属于同一研究。付费墙、JavaScript 页面与 PDF 复杂版式依旧可能不可读。
