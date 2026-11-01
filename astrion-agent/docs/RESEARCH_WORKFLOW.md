# Astrion 科研能力：分阶段实现与复现

本次交付从现有 ReAct、ToolSpec / ToolRegistry、工具注入、五层上下文、证据归档和 Vue / FastAPI 扩展。默认仍为 `single` 决策；不默认启用多次模型复核。固定任务在 `fixtures/research/cases.json`，示例资料为合成验收材料，不作为真实科学证据。

## 安装与原有数据

在 `astrion-agent/` 执行：

```bash
.venv-ui/bin/python -m pip install -r requirements-research-test.txt
bash run_tests.sh
bash run_research_tests.sh
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
tools/research.py           角色工具装配；主 Agent 额外获得 run_worker / run_workers
agent.py                   循环、状态、调用验证、事件和上下文调度
research/runner.py         科研编排与根 Agent 生命周期
research/workers.py        有界并发、独立客户端/连接、用量汇总
research/continuation.py   检查点复制、上下文和任务状态继承
research/config.py         CLI / API / 运行器共享研究预算
research/prompts.py         研究与收尾提示
research/citations.py       引用位置检查（不等于语义支持）
research/store.py           兼容资料与记忆入口
research/storage/          持久化、迁移、位置与分页
research/ingestion/         TXT/MD/PDF 与 OCR 适配
research/retrieval/         关键词、向量适配与融合排序
research/routes/           HTTP 校验、项目边界和响应转换
research/runs.py           持久化 Event v1、版本结果、项目内历史读取
frontend/src/components/   独立资料交互，主界面负责组合
```

业务工具不导入 `agent.py`；核心循环不导入科研存储。新工具先在对应能力模块添加 ToolSpec 与实现，再在 `tools/research.py` 装配适用角色；结构化证据需在 `memory_evidence.py` 添加明确适配。契约不在 CLI/API 重新定义。API 和前端只调用业务入口。

每次研究在资料数据库旁的 `runs/<project_id>/<run_id>/` 保存 `events.jsonl` 与 `result.json`。最终结果分别提供结束状态、引用位置检查、耗时和模型 usage。语义支持仍需独立的逐结论原文核查；不以 `locations_found` 或模拟模型测试冒充研究质量提升。

### 第一阶段实测记录（2026-11-01）

- 原有离线 210 项、新资料行为 7 项、新资料 API 1 项、原有 API/HTTP 4 项通过；Node 18 前端生产构建通过。
- 真实多语言编码器：中文问题召回英文 PDF 方法页，语义通道为 available；此小样例不能代表全面检索效果。
- 真实 Tesseract：扫描样例提取到 128 和 81，并保留 OCR 未验证提示。三页 PDF 已渲染目视核对。
- `stage1-acceptance.json`：真实模型主动完成，6 轮决策，总计 7 次调用（含摘要），Agent 运行36.4秒（含导入验收共37.7秒）、29,447 token。跨资料引用位置可核对；人工抽查 RAG 定义、协作风险及扫描缺失描述得到来源支持。回答开头对中英文主题覆盖的概括偏强，后文有所限定；未给出全量语义通过率，也未声称比旧版本质量提高。
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

网络层独立在 `research/providers/http.py`：只允许公共 HTTP(S) 80/443，固定经校验的 DNS 地址并保留 TLS 主机名校验，重定向逐次重新验证且跨来源不传凭据，最多 5 跳；读取大小最多 20 MB。网页请求约 15 秒、元数据请求约 25 秒（DNS 解析和操作系统连接可能额外耗时，并非完整任务硬截止）；限流只在 Retry-After 不超过 1 秒时额外重试一次，其余交还结构化错误。错误区分访问拒绝、DNS、网络、超时、限流、过大内容、提取失败。不会把失败页面当成已读全文，也不绕过登录或付费墙。

新增模块：`research/providers/{http,literature,web}.py` 负责外部适配，`research/storage/sources.py` 负责快照，`tools/external.py` 负责声明和绑定，`research/routes/external.py` 负责 API，`ExternalSources.vue` 负责交互。新服务只需实现同样的 search / metadata 或 fetch 接口并在装配入口替换。

参考契约：[Crossref REST API](https://github.com/CrossRef/rest-api-doc)、[pypdf 提取边界](https://pypdf.readthedocs.io/en/6.21.1/user/extract-text.html)、[FastEmbed 多语言模型](https://qdrant.github.io/fastembed/examples/Supported_Models/)。

### 第二阶段验证与限制（2026-11-01）

- 外部服务/错误/快照/API 回归 7 项通过；此前资料与原有接口回归一并通过，前端构建通过。
- 真实 Crossref 搜索暴露了默认游标顺序的问题，已显式添加 score 降序排序；真实 DOI、公开 arXiv PDF 和摘要页可获取。网络验证也实际遇到过 DNS 失败和请求超时，均得到明确错误。
- arXiv DOI 通常由 DataCite 注册：已补 `research/providers/doi.py`，仅在 Crossref 返回 404 时回退 DataCite；不把超时、限流或权限失败吞掉。
- 第一次阶段验收耗时87.1秒，其中 Agent 运行81.2秒；模型 7 轮决策、3 次摘要，共 49,992 token，预算收尾为 `no_tool_call`。读取了 RAG 原论文 PDF 开头和另一篇综述的摘要页，生成限定为摘要级的比较；没有读全两篇论文，不能标为完整全文研究完成。该运行发生在 DataCite 回退及摘要页专门分类补丁之前。
- 手工核查发现回答的图 1 说明引用范围过窄，位置检查仍可通过。因此本次证据支持只算部分核查，语义裁判和来源绑定问题仍按根目录 PROBLEMS.md 跟踪，不宣称已全部解决。
- Crossref 查询会返回书籍、预印本等多种记录，并可能存在同题不同 DOI；当前按 DOI 去重，尚未自动判定不同版本属于同一研究。付费墙、JavaScript 页面与 PDF 复杂版式依旧可能不可读。

## 第三阶段：计算与产物交付

`list_artifacts`、`read_project_file`、`write_project_file`、`execute_python` 已装配到研究主 Agent 和子 Agent。两者只能操作当前项目的产物；子 Agent 仍无再次委派权限。资料、网页和产物正文始终是数据。长期记忆仍由用户显式保存，研究工具没有修改记忆的权限。

产物具有不可变 ID、SHA-256、项目归属、创建时间及来源元数据。物理文件位于资料数据库旁的 `projects/<project_id>/artifacts/<artifact_id>/<name>`，索引在 SQLite `artifacts` 表；同名保存产生新版本，不覆盖原件。上传上限 8 MB，项目产物限额 256 MB / 2000 个。API、模型工具均接受产物 ID，不接受宿主路径。越项目、路径穿越、符号链接和内容哈希不一致会被拒绝。CLI 导出文件使用独占创建，不覆盖已有文件。

计算服务将选中的最多 10 个输入文件（合计 16 MB）准备到临时目录，只读挂载到容器 `/inputs`。脚本保存为 `analysis.py`；代码从 `/inputs/文件名` 读数据，把输出写在 `/work` 根目录。执行结果记录 `status`、`exit_code`、stdout/stderr、截断标志、错误码、输入哈希、镜像 ID、耗时、脚本和产物 ID。`execution.json` 永久保留该次结果；失败及超时也留档。`unavailable`、`timeout` 或非零退出不能解释为计算成功。工具调用本身成功送达与 Python 执行成功是两个状态。

根研究结束时自动保存 `research-report.md`，标记为未经独立语义验证的模型报告。工作台增加“数据与产物”：上传 CSV/JSON 等数据、分页查看文本、显示 PNG/JPEG 图表、下载脚本/报告/结果，以及手工提交 Python。主研究页也提供报告下载。下载仅按当前项目查询产物，使用 attachment 与 nosniff；不直接托管任意 HTML/SVG。此项目仍为本机单用户应用，项目隔离不是多用户身份认证。

### 真实隔离边界与安装

实现仅使用 Docker 适配器，**没有普通宿主 Python 子进程兜底**。Docker 不能用、镜像缺失或 daemon 未启动时返回 `sandbox_unavailable`，保留脚本与失败记录。当前开发机器没有 Docker/Podman；系统 sandbox-exec 也返回 Operation not permitted，因此本机没有执行真实容器验收。下面的容器构建及执行流程已提供，但本次不能报告为已在本机验证成功。

安装并启动 Docker 后，在 `astrion-agent/` 执行：

```bash
docker build -t astrion-research:1 research/execution
.venv-ui/bin/python -m research execution-status --project 1
ASTRION_TEST_DOCKER=1 .venv-ui/bin/python -B -m unittest tests.research.test_docker_live -v
.venv-ui/bin/python -m research.acceptance --stage 3 --output research_data/stage3-with-docker.json
```

镜像的 Python、numpy、pandas、matplotlib 版本在 Dockerfile 中列明；运行记录保存实际镜像内容 ID。重新构建仍可能取得变化的系统依赖，严格重复实验应保留实际镜像，而不只保留标签。构建阶段需要下载受信依赖；研究代码运行阶段无网络，不能动态 pip 安装依赖。可由用户构建审核后的镜像，使用根 `.env` 的 `ASTRION_EXECUTION_IMAGE` 选择。

执行限制：

| 边界 | 实现与限制 |
|---|---|
| 文件 | 非 root UID 65534；只读根文件系统与输入挂载；只挂选中输入，不挂仓库、Docker socket、宿主家目录或 `.env` |
| 网络 | `--network=none`；没有用户代码可切换的联网选项 |
| 权限 | drop ALL capabilities、no-new-privileges、Docker 默认 seccomp 与命名空间；不是 microVM，也未证明可抵御所有内核漏洞 |
| 内存与 CPU | 512 MB 内存/交换总量、1 CPU、64 PID、128 文件描述符；取决于 Docker daemon 正确提供隔离和 cgroup 支持 |
| 时间 | Python 1–60 秒，默认 20；外层另给启动/收集 5 秒并强制清理容器；镜像检查和清理各有独立短超时。这不是整个科研任务的时间上限 |
| 磁盘 | `/work` 32 MiB tmpfs、`/tmp` 16 MiB tmpfs；不向宿主开放可写目录；单文件 RLIMIT_FSIZE 为 4 MB |
| 输出 | stdout/stderr 各最多返回 6000 字符并标记截断；最多20个根目录常规文件，每个4 MB、合计12 MB；仅受支持扩展名；不收集目录/符号链接；宿主协议流上限18 MB |
| 凭据 | 容器只获得镜像默认环境和显式非敏感环境；不传入宿主模型 key / AWS key。Docker 客户端只保留其连接所需变量 |

执行适配器退出码与产物是运行事实，不证明算法、实验设计或科学结论正确。用户代码可以生成错误文件，独立验收仍须重新核算。输出超限、容器 OOM 或协议异常会失败；不会把部分产物默认为完整成果。安全边界依赖受信 Docker daemon 和镜像，不建议把当前本机单用户工作台直接暴露给不受信多租户。

### 可复现的数据分析流程

```bash
# 创建新的项目，下面将返回 ID 代入 1
.venv-ui/bin/python -m research project-add analysis-demo
.venv-ui/bin/python -m research artifact-add --project 1 fixtures/research/measurements.csv
# 将返回的产物 ID 代入 INPUT_ID
.venv-ui/bin/python -m research execute --project 1 fixtures/research/analysis.py --input INPUT_ID --timeout 30
.venv-ui/bin/python -m research artifacts --project 1
.venv-ui/bin/python -m research artifact-read --project 1 RESULT_JSON_ID
.venv-ui/bin/python -m research artifact-export --project 1 CHART_ID chart.png

# 再交给真实 Agent 自主调用工具（已有模型 .env）
.venv-ui/bin/python -m research run --project 1 --steps 10 '读取 measurements.csv，按组计算均值，生成图表、可复现脚本和包含数据来源的报告。根据真实执行结果回答。'
```

固定验收 `fixtures/research/analysis.py` 使用四行合成 CSV：独立参考均值 A=3、B=6。容器执行成功后，验收核对 result.json、输入哈希、PNG 格式、脚本、报告来源；生产研究仍需另行审核数据与结论。输出 JSON 内的 `checks.task_completed` 才是此固定任务的完成检查；命令能够保存报告不代表任务完成。

### 第三阶段实测与剩余限制（2026-11-01）

- 真实模型跑通了工具装配、读取项目数据、提交代码、接收不可用状态、归档与报告保存链。5 轮决策、1 次摘要，26.9 秒，27,960 token（输入24,788、输出3,172）；结束为 `incomplete_plan`。
- 模型没有谎称已算出结果，但一次计划参数使用了不支持的 `in_progress`，被契约拒绝；预算收尾出现 DSML 文本形式的工具调用，未执行，也没有形成可用分析报告。该次失败完整保留在 `research_data/stage3-acceptance.json`，不当作模型效果提升。
- 固定执行返回 `sandbox_unavailable`，因此**尚无真实隔离环境中的统计、图表和资源限制通过记录**。四项真实 Docker 测试显式跳过；配好 Docker 后用上面同一验收命令补测。离线适配器替身只验证接线和错误传播。
- 当前产物无删除界面/自动清理策略；配额耗尽会拒绝新增。论文表格/公式恢复、付费全文、复杂动态网页、通用语义裁判、完整任务恢复和取消不在这次实现范围。

## 最终目录与依赖方向

```text
tooling.py                    公共契约唯一权威
agent.py                      原有循环、状态及模型客户端；兼容导出原工具
tools/
  contracts.py                小型声明/输出帮助函数，复用 ToolSpec
  builtin.py                  原有基础工具与控制声明
  knowledge.py                资料列表、目录、检索、阅读、记忆搜索
  external.py                 文献/DOI/网页工具
  computation.py              项目文件与执行工具
  research.py                 唯一研究角色装配入口
research/
  runner.py, prompts.py        科研编排与提示；不执行供应商细节
  citations.py, runs.py        位置核对、运行事实持久化
  store.py, storage/          项目、记忆、文档、快照、产物存储
  ingestion/, retrieval/      提取/OCR、关键词/语义适配与排序
  providers/                  HTTP、Crossref、DataCite、正文提取
  execution/                  计算业务服务、Docker 适配、容器入口和镜像
  api.py, routes/             FastAPI 请求校验与响应转换
  __main__.py, commands/      兼容 CLI 与能力命令组
frontend/src/components/     SourceLibrary / ExternalSources / Artifacts
fixtures/research/            三阶段固定资料、任务及分析脚本
tests/research/               资料、外部服务、执行、交付与可选真容器测试
```

依赖方向：前端 → API/CLI → 业务服务 → 存储/外部适配；研究 runner → 工具装配 → 工具实现 → 同一业务服务。核心循环通过 ToolSpec 和注入实现调度，不依赖科研业务。工具声明不在 API/CLI 复制；两者的请求结构只服务各自协议。研究服务不导入 API 或前端。已有大文件按职责抽出工具、提示、引用核查、存储、路由；未重写基础循环与模型协议。

增加一种工具：在合适的 tools 能力模块定义声明与绑定方法；业务复杂时放到 research 对应服务/适配模块；在 tools/research.py 加入适用角色；需要精确保留的证据在 memory_evidence.py 添加适配；再按需加 CLI/API/UI 和同职责测试。新执行后端只需提供 availability / execute；新向量后端提供 name / encode，外部文献适配提供 search / metadata。当前没有强行引入 MCP 或额外编排框架。

## 验证入口与结果口径

```bash
bash run_tests.sh                 # 标准库核心回归（210项）
bash run_research_tests.sh        # 科研扩展、API/本机HTTP；默认不调用模型和公网
npm --prefix frontend run build  # Node 18+
# 真实容器验证必须显式打开，未安装会失败而不是变成通过
ASTRION_TEST_DOCKER=1 .venv-ui/bin/python -B -m unittest tests.research.test_docker_live -v
```

本次核心210项通过；科研扩展34项中30项通过、4项容器实测跳过；原有API/HTTP4项通过。前端构建通过。真实模型与外部服务是独立的验收记录，不混进离线通过率。所有阶段记录任务完成、证据支持状态、耗时与 token；`citation_audit` 只检查位置（已兼容单行引用），不是正确率。固定验收 JSON、原文快照、产物及完整事件都在忽略的 research_data 中，不上传私有数据。

Docker约束依据：[Docker security](https://docs.docker.com/engine/security/)、[resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)。

工作台实测：独立验收项目完成 CSV 上传、预览和浏览器下载（与原文件字节相同）；PDF 目录/第3页缺失标记与 DOI 元数据查询均正常。原项目、文档和记忆的原有列哈希核对不变；迁移前备份位于 research_data/astrion.before-research-extension-20261101.sqlite3。GitHub Actions 增加研究依赖、真容器和前端构建任务，但本次尚未远程触发，不能据此宣称 CI 已通过。

## 输出异常修复（2026-11-01）

DSML 控制文本不再当作回答保存；最后一轮以只读历史快照收尾，异常返回 error，原始日志保留。目录按约2600字符分页，必须遵循 next_offset；详情与最新255项通过/4项跳过口径见 [P0事故记录](incidents/2026-11-01-protocol-as-report.md)。此前三阶段实测数据是历史记录，并未改写为修复后的成绩。

## 持续研究与并发

默认主/子 Agent 各50轮、每次运行最多5个子 Agent，支持批量并发与根上下文续写。每次另存报告版本并独立计量；完整命令、状态语义和验证记录见 [RESEARCH_CONTINUATION.md](RESEARCH_CONTINUATION.md)。
