# Astrion

*Intelligence bound for the stars.*

Astrion is an agent project grounded in science, guided by imagination, and driven by optimism.
Its current focus is research assistance: building agents that work with project documents, expose their execution traces, and can be evaluated independently.

## Features

- **Agent loop:** structured tool calls, argument validation, explicit task states, step budgets, and execution events.
- **Two-level agent tree:** a parent agent delegates tasks to children with separate contexts. Child agents cannot delegate further.
- **Documents and memory:** SQLite stores project documents and explicitly saved long-term memories. Conversation memories are shared across projects; project documents and memories remain scoped to their project.
- **Five-layer context management:** tool output budgets, duplicate-history trimming, microcompaction, local folding, and global summarization. Durable evidence can be searched, reread, and pinned independently of the active context.
- **Research answers:** keyword retrieval, reading source text by line range, source citations, and citation location checks.
- **Research workspace:** Vue 3 and Bootstrap frontend, FastAPI backend, SSE streaming, and Markdown report rendering.
- **Evaluation:** independent post-run judges, duration and token accounting, aggregate reports, and BFCL integration for comparing the official baseline with Astrion.

Document import currently supports UTF-8 `.txt` and `.md` files up to 1 MB each. Retrieval uses keyword matching. Citation checks verify that referenced lines were read, but do not assess whether those lines support a claim. Child agents run sequentially. Automatically writing and running research experiments is a future direction.

User requests remain verbatim. Selected evidence quotes and task-plan state stay in context; other observations remain accessible in the archive. These mechanisms preserve recorded information, not the truth of a summary or answer. Child reports remain unverified observations.

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
.venv-ui/bin/python -m pip install -r requirements-ui.txt
.venv-ui/bin/python -m uvicorn research.api:app --host 127.0.0.1 --port 8000
```

In another terminal, start the frontend from your cloned repository:

```bash
cd Astrion/astrion-agent/frontend
npm ci
npm run dev
```

Open the local URL printed by Vite, usually `http://127.0.0.1:5173/`. Create a project in the sidebar, import documents into its library, and submit a question in the research workspace. The current UI is in Chinese.

The backend reads the model API key. Documents and memories are stored in `astrion-agent/research_data/astrion.sqlite3` by default. The workspace is intended for local, single-user use and does not provide authentication. Disconnecting the page does not cancel an ongoing backend task.

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
.venv-ui/bin/python -m pip install -r requirements-ui-test.txt
.venv-ui/bin/python -m unittest test_research_api test_research_http
```

Build the frontend from the repository root:

```bash
cd astrion-agent/frontend
npm run build
```

With the frontend development server running, open `/tests/markdown.html` to check Markdown formatting, partial streaming content, and unsafe input handling.

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
    ├── context_tools.py     Search, reread, and pin tools
    ├── context_adapter.py   Optional server contract and client fallback
    ├── context_service.py   Runnable reference context-editing service
    ├── research/            Research agents, SQLite, API, and streaming
    ├── frontend/            Vue 3 research workspace
    ├── evaluation/          Independent evaluation layer
    ├── benchmarks/          BFCL adapters and comparisons
    └── run_tests.sh         Offline test entry point
```

See the [agent guide](astrion-agent/README.md) for detailed commands, the [benchmark guide](astrion-agent/benchmarks/README.md) for BFCL integration, and the [architecture decision record](docs/adr/0001-tool-contract-and-task-state.md) for tool contracts and task states. These detailed documents are currently in Chinese.

`.gitignore` excludes `.env`, local research data and databases, virtual environments, dependency directories, and benchmark run artifacts.
