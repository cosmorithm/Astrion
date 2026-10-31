# Astrion

*Intelligence bound for the stars.*

Astrion is an agent project grounded in science, guided by imagination, and driven by optimism.
Its current focus is research assistance: building agents that work with project documents, expose their execution traces, and can be evaluated independently.

## Features

- **Agent loop:** structured tool calls, argument validation, explicit task states, step budgets, and execution events.
- **Two-level agent tree:** a parent agent delegates tasks to children with separate contexts. Child agents cannot delegate further.
- **Documents and memory:** SQLite stores project documents and explicitly saved long-term memories. Conversation memories are shared across projects; project documents and memories remain scoped to their project.
- **Research answers:** keyword retrieval, reading source text by line range, source citations, and citation location checks.
- **Research workspace:** Vue 3 and Bootstrap frontend, FastAPI backend, SSE streaming, and Markdown report rendering.
- **Evaluation:** independent post-run judges, duration and token accounting, aggregate reports, and BFCL integration for comparing the official baseline with Astrion.

Document import currently supports UTF-8 `.txt` and `.md` files up to 1 MB each. Retrieval uses keyword matching. Citation checks verify that referenced lines were read, but do not assess whether those lines support a claim. Child agents run sequentially. Automatically writing and running research experiments is a future direction.

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
    ├── research/            Research agents, SQLite, API, and streaming
    ├── frontend/            Vue 3 research workspace
    ├── evaluation/          Independent evaluation layer
    ├── benchmarks/          BFCL adapters and comparisons
    └── run_tests.sh         Offline test entry point
```

See the [agent guide](astrion-agent/README.md) for detailed commands, the [benchmark guide](astrion-agent/benchmarks/README.md) for BFCL integration, and the [architecture decision record](docs/adr/0001-tool-contract-and-task-state.md) for tool contracts and task states. These detailed documents are currently in Chinese.

`.gitignore` excludes `.env`, local research data and databases, virtual environments, dependency directories, and benchmark run artifacts.
