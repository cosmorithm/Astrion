# Astrion

*Intelligence bound for the stars.*

Astrion is an agent project grounded in science, guided by imagination, and driven by optimism.
Its current focus is research assistance: building agents that work with project documents, expose their execution traces, and can be evaluated independently.

## Features

- **Agent loop:** structured tool calls, argument validation, explicit task states, step budgets, and execution events.
- **Two-level agent tree:** a parent delegates independent tasks concurrently, with separate model clients, database connections and contexts. Research defaults to 50 decision rounds per agent and at most five children per run; children cannot delegate further.
- **Documents and memory:** SQLite stores project documents and explicitly saved long-term memories. Conversation memories are shared across projects; project documents and memories remain scoped to their project.
- **Five-layer context management:** tool output budgets, duplicate-history trimming, microcompaction, local folding, and global summarization. Durable evidence can be searched, reread, and pinned independently of the active context.
- **Research evidence:** PDF/OCR ingestion, page/section/line reading, multilingual semantic and keyword retrieval, Crossref/DataCite metadata, public webpage snapshots, and citation location checks.
- **Computation and delivery:** project-scoped data and immutable artifacts, a Docker execution adapter, downloadable scripts/results/charts/reports, and persistent run journals. Execution fails closed without a usable container runtime.
- **Research workspace:** Vue 3 and Bootstrap frontend, FastAPI backend, SSE streaming, Markdown report rendering, version history and checkpoint-based continuation.
- **Evaluation:** independent post-run judges, duration and token accounting, aggregate reports, and BFCL integration for comparing the official baseline with Astrion.

Document import supports UTF-8 `.txt` / `.md` (1 MB) and PDF (25 MB / 300 pages), with page locations, extraction warnings and optional OCR. Retrieval combines exact keywords with an optional local multilingual encoder. Citation checks verify that referenced lines were read, but do not assess whether those lines support a claim. Independent child tasks can run concurrently in a bounded batch (up to five children per research run). Python analysis is wired into research agents and the workspace. The current development machine has no Docker runtime, so real container execution and isolation checks remain unverified; mocked adapter tests do not establish sandbox security or scientific validity.

User requests remain verbatim. Selected evidence quotes and task-plan state stay in context; other observations remain accessible in the archive. These mechanisms preserve recorded information, not the truth of a summary or answer. Child reports remain unverified observations.

### Research exam

Astrion Research Eval v1 uses a local, frozen set of 25 original PDFs, 20 questions and separate development/holdout paper sets. It runs isolated single- and multi-agent research tasks with independent post-run checks. Answer keys and semantic judgments remain provisional until reviewed against the PDFs; the included small pilot is a connectivity test, not a benchmark score. See the [exam guide](astrion-agent/evaluation/research/README.md).

## Five-layer context pipeline

Before a decision, Astrion checks the message character budget and individual tool output sizes. The following layers run in order when applicable; a layer that does not reduce the view is not committed. Lightweight layers can avoid subsequent summary calls.

| Layer | Trigger and behavior | Model call |
|---|---|---|
| 1. Tool Result Budget | Externalize results over 3,000 characters; keep a preview and content-addressed references. Bounded `read_evidence` pages are exempt so rereading actually delivers the text. | No |
| 2. History Snip | Remove consecutive, identical, successful read-only batches without assistant commentary. Original messages remain stored. | No |
| 3. Microcompact | Under pressure, replace older successful result bodies over 500 characters with receipts. Preserve calls, IDs, and the most recent four non-user units. | No, by default |
| 4. Context Collapse | Summarize up to four older non-user units into a local block, retaining its archived source and recent units. | When applicable |
| 5. Auto-compact | If pressure remains, rebuild the working summary while retaining user requests, pinned quotes, task-plan state, and at least the latest complete unit. | When applicable |

Summaries require nonempty `progress` and `pending` fields and the exact set of pinned `evidence_ids`. Missing, invented, or duplicate IDs reject that summary. This is structural validation, not semantic verification. Each layer commits separately; a failed later layer does not undo valid earlier reductions. An invalid summary cannot delete its source archive. Summary API failures propagate through the existing run-error path.

The default 12,000-character budget measures serialized messages, not model tokens or tool schemas. It is a soft target: if protected or recent content alone exceeds it, `over_budget` remains true. The pipeline does not guarantee a provider's token limit. Providers' reported usage still accounts for decision and summary calls.

### Storage and evidence tools

- Project documents and explicit long-term memories: `astrion-agent/research_data/astrion.sqlite3`.
- Original observations, message journals, and context checkpoints: `astrion-agent/research_data/context.sqlite3`. Research mode places this beside its configured research database; the core CLI accepts `ASTRION_CONTEXT_DB`.
- Every parent and child receives a separate archive scope. References resolve only inside the current scope. Content hashes identify immutable observations; a changed source creates another observation.
- `search_evidence(query, offset)` finds archived references, five matches per page. `read_evidence(ref, offset, limit)` reads exact text, up to 4,000 characters per page. `pin_evidence(ref, quote)` keeps a verified contiguous source quote in context; an empty quote explicitly unpins it. Quotes are limited to 1,000 characters each and 2,000 serialized characters in total.

Tool output includes `_context.result_ref` and, for recognized evidence formats, `_context.evidence_refs`. The archive preserves what the tool actually returned, including truncation markers; it cannot recover text a tool never returned. Pin selection is made by the caller or agent, so selection completeness is not guaranteed.

Research runs persist context checkpoints after complete tool batches and expose their archive path and scope in the result. `Memory.from_archive(path, scope)` restores the last checkpoint and its runtime metadata. Existing `--session` JSON snapshots remain supported and include archive contents for portability. `Memory(...)` without an archive is in-memory for library use; application entry points attach persistent storage. Archives have no automatic retention or cleanup policy yet and are excluded from Git.

### Optional server adapter

Default operation is entirely client-side and independent of the model vendor. Set `ASTRION_CONTEXT_SERVICE_URL` to enable the HTTP v1 adapter at layer 3; an optional dedicated key and a timeout greater than zero and at most 30 seconds are configurable. The service receives only eligible result IDs, tool names, lengths, and a character budget. It returns `{"version":1,"clear_ids":[...]}`; unknown or duplicate IDs, errors, and timeouts fall back to client rules. It cannot rewrite user messages or source evidence.

This is a separate context-editing service contract, **not native Claude cache editing or an assumed Chat Completions extension**. A deterministic reference service is included:

```bash
cd astrion-agent
python3 -m context_service --port 8766
```

Then set `ASTRION_CONTEXT_SERVICE_URL=http://127.0.0.1:8766/context-edits` in the root `.env`. If using a key, start the service with the same `ASTRION_CONTEXT_SERVICE_KEY` environment variable. The reference service does not load `.env` itself. Other implementations can supply the same contract without changing the model client.

`memory.compaction.finished` includes five layer reports with before/after character counts, decisions, affected units or IDs, and adapter fallback information. Validate with `python3 -B -m unittest test_memory test_context_pipeline test_session test_research` from `astrion-agent/`; the HTTP integration tests also exercise the real optional service protocol.

Leaked tool-control syntax is rejected at the answer boundary and is not published as a report. See the [2026-11-01 incident and fix](astrion-agent/docs/incidents/2026-11-01-protocol-as-report.md).

## Research workflow

See [the staged research workflow](astrion-agent/docs/RESEARCH_WORKFLOW.md) for PDF/OCR import, page and section reading, multilingual retrieval, external literature evidence, isolated computation, architecture, actual validation results and reproducible acceptance commands.

## Quick start

Requirements: Python 3.10+, Node.js 18+, and a model service with a compatible Chat Completions API.

```bash
git clone https://github.com/cosmorithm/Astrion.git
cd Astrion
cp .env.example .env
```

Edit `.env` in the repository root and enter your API key. The example uses `deepseek-flash`; adjust the model name and base URL for your provider.

Start the backend:

```bash
cd astrion-agent
python3 -m venv .venv-ui
.venv-ui/bin/python -m pip install -r requirements-research.txt
.venv-ui/bin/python -m uvicorn research.api:app --host 127.0.0.1 --port 8000
```

In another terminal, start the frontend from your cloned repository:

```bash
cd Astrion/astrion-agent/frontend
npm ci
npm run dev
```

Open the local URL printed by Vite, usually `http://127.0.0.1:5173/`. Create a project in the sidebar, import documents into its library, and submit a question in the research workspace. The current UI is in Chinese.

The backend reads the model API key. Use the “外部证据” tab to search/import sources and “数据与产物” to upload data or preview/download outputs. Documents and memories are stored in `astrion-agent/research_data/astrion.sqlite3` by default. The workspace is intended for local, single-user use and does not provide authentication. Disconnecting the page does not cancel an ongoing backend task.

### Research trace snapshots and model context

The workbench offers **查看每轮输入输出** inside the research activity panel. Select a project and an existing research run, then expand individual model and tool events; historical events load in pages. New research runs persist the actual model input messages and tool declarations, the model's returned response, tool calls/results and compaction events in the run's `events.jsonl`. The model can retrieve previous project runs and paged event snapshots through `list_research_runs` and `read_research_event`: these are durable project-scoped historical data, not automatically verified research evidence or shared conversation memories. Older runs did not capture complete model inputs; the UI explicitly marks them unavailable rather than reconstructing them. Snapshots can contain private document text and provider-exposed reasoning; keep the local files private and include the run directory in backups.

DeepSeek's [model metadata](https://api-docs.deepseek.com/api/list-models/) lists a 1,048,576-token context for `deepseek-flash`. For the official endpoint and recognized 1M models, research defaults to a **600,000-character budget**, adjustable in the workbench or `--context-budget-chars`; unknown providers retain the 12,000-character default. Persistent runs also use that number as a separate local token input budget unless explicitly configured otherwise. With that budget, SS reconstruction uses the token watermark; legacy retains its existing character and token triggers. Characters are not tokens, and the local JSON serialization does not guarantee the provider's hidden template fits the model window. Tool schemas, completion tokens, pinned material, compression behavior and provider tokenization affect actual use. A larger threshold can substantially increase per-request cost and storage. Other providers' context windows are marked unknown unless verified.

### Continue a research report

Select a previous run in **研究历史**, enter the additional instructions, and click **继续研究**. After a successful response, subsequent requests continue from that new version; select **新建研究** to start independently. Each continuation forks the saved root context, pinned evidence and task plan into a new archive scope and saves a new report artifact. Previous reports remain unverified drafts. Citation location checks include actual reads inherited from the parent run, not merely citations written in an old report.

From `astrion-agent/`:

```bash
.venv-ui/bin/python -m research runs --project 1
.venv-ui/bin/python -m research run --project 1 --continue-run research_REPLACE_WITH_RUN_ID \
  "Continue reading uncovered papers and produce a complete revised report with source citations."
```

Both root and child agents default to **50 decision rounds**, with **five children total per run**. The root can use `run_workers` to execute independent tasks concurrently and wait for their results; `run_worker` remains available for sequential dependencies. Children get no delegation tools. These are ceilings, not mandatory work: agents can finish early. `--steps`, `--worker-steps`, `--workers` and the workspace budget controls can reduce them. Ordinary core CLI and BFCL defaults are unchanged.

Each continuation has a fresh budget and reports its own duration and total provider-reported usage, including all child and compaction calls. It restores the root checkpoint, not a live child thread or an interrupted side effect. A missing/corrupt checkpoint is rejected. History is project-scoped and reloadable after a browser restart; there is no mid-stream cancellation or crash-safe tree replay. Full child reports are stored as artifacts; batch summaries are bounded and explicitly mark truncation. See [the continuation guide](astrion-agent/docs/RESEARCH_CONTINUATION.md) for contracts, limitations and verification.

### Enable isolated computation

From `astrion-agent/`, with Docker installed and running:

```bash
docker build -t astrion-research:1 research/execution
ASTRION_TEST_DOCKER=1 .venv-ui/bin/python -B -m unittest tests.research.test_docker_live -v
.venv-ui/bin/python -m research.acceptance --stage 3 --output research_data/stage3-with-docker.json
```

Execution has no network, no host credentials, read-only selected inputs, a 512 MB memory limit and a 1–60 second code deadline. Code runs in bounded container tmpfs; output files are collected into the project artifact store. These are container boundaries, not a microVM or a proof against all kernel vulnerabilities. Only trusted prebuilt images are used; there is no host-process execution fallback. The task deadline is separate from the research agent's step budget.

Research snapshots remain in SQLite, artifacts live beside the database under `projects/<project_id>/artifacts/`, and complete events/results live under `runs/<project_id>/<run_id>/`. Back up both the database and sibling data directories when moving existing projects.

## CLI and tests

Run the core agent from `astrion-agent/`:

```bash
python3 agent.py "Read data.txt, sum all the numbers, and write the result to result.txt."
```

Run the offline test suite from the repository root. No model API key is required:

```bash
bash astrion-agent/run_tests.sh
```

Run the research API and local HTTP streaming integration tests:

```bash
cd astrion-agent
.venv-ui/bin/python -m pip install -r requirements-research-test.txt
bash run_research_tests.sh
```

Build the frontend from the repository root:

```bash
cd astrion-agent/frontend
npm run build
```

With the frontend development server running, open `/tests/markdown.html` to check Markdown formatting, partial streaming content, and unsafe input handling.

## Per-agent context history

CLI and research runs persist each agent's own execution steps, observable events, and model input snapshots in the scoped context SQLite database. Research workers have separate scopes; continuation forks the root history into a new scope and does not import worker-private history. Research history APIs expose agents and their steps under `/api/projects/{project_id}/runs/{run_id}/agents`; the detailed [agent guide](astrion-agent/README.md) includes pagination, privacy, and recovery details.

The CLI, research parent, and research workers use the five-layer `legacy` compaction pipeline by default. Each agent retains its own persisted history, snapshots and evidence. Continuing an existing session keeps its archived history and existing summaries; future compactions use the five-layer pipeline. The SS planner remains available only when explicitly selected by the Python API or comparison experiments. Local token budgets remain enforced before model requests; five-layer compaction is not guaranteed to shrink every request below that limit. See the [agent guide](astrion-agent/README.md) and [long research evaluation](astrion-agent/evaluation/long_research/README.md) for details. Local counts use a fixed JSON representation and do not guarantee the provider uses the same hidden chat template.

New research runs expose bounded source excerpts for individual citations, with the reading tool and agent identity, in the report's evidence panel. A tool returning text does not prove the model saw all of it after compaction, and a matching location does not prove semantic support. Older reports may lack excerpts. The [evidence review](astrion-agent/evaluation/research/EVIDENCE_REVIEW.md) records the initial real-model samples and a scoring change for citation-shaped examples.

## Repository layout

```text
Astrion/
├── README.md
├── .env.example
├── .github/workflows/       Offline test CI
├── docs/adr/                Architecture decision records
└── astrion-agent/
    ├── agent.py             Agent loop and model client
    ├── tooling.py           Tool contracts and validation
    ├── memory.py            Session context and compaction
    ├── memory_evidence.py   Tool observation preservation adapters
    ├── context_store.py     Scoped journals, artifacts, and checkpoints
    ├── context_history.py   Per-agent steps, snapshots, summaries and builds
    ├── context_construction.py  On-demand SS catalog and validated reconstruction
    ├── context_tokenizer.py     Local token counter and pinned tokenizer installer
    ├── context_tools.py     Search, reread, and pin tools
    ├── context_adapter.py   Optional server contract and client fallback
    ├── context_service.py   Runnable reference context-editing service
    ├── tools/               Capability declarations, bindings and role assembly
    ├── research/            Orchestration, storage, ingestion, retrieval, providers, execution and API
    ├── fixtures/research/   Fixed acceptance documents, data and analysis script
    ├── tests/research/      Research regression and opt-in real Docker tests
    ├── frontend/            Vue 3 research workspace
    ├── evaluation/          Independent evaluation layer
    ├── benchmarks/          BFCL adapters and comparisons
    └── run_tests.sh         Offline test entry point
```

See the [agent guide](astrion-agent/README.md) for detailed commands, the [benchmark guide](astrion-agent/benchmarks/README.md) for BFCL integration, and the [architecture decision record](docs/adr/0001-tool-contract-and-task-state.md) for tool contracts and task states. These detailed documents are currently in Chinese.

`.gitignore` excludes `.env`, local research data and databases, virtual environments, dependency directories, and benchmark run artifacts.
