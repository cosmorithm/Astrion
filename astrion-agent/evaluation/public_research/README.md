# Public research benchmarks

These integrations evaluate the real Astrion research loop. Run them from `astrion-agent/`. Datasets, private answers, workspaces, model logs and results live under `research_data/public_benchmarks/` and are not committed. Logs can contain research content. Do not publish BrowseComp questions or answers in plain text.

## QASPER v0.3

QASPER comprises research papers, questions, reference answers and annotated evidence. This adapter takes the first question from each selected paper in the official dev or test order, imports that paper's structured text into a new isolated project for each task, and gives the question—but never the answer or evidence annotations—to `run_research`. It uses the official, unmodified `qasper_evaluator.py` from the official archive.

```bash
cd "/Users/apple/Desktop/科研/Astrion/astrion-agent"
.venv-ui/bin/python -m evaluation.public_research qasper-prepare
.venv-ui/bin/python -m evaluation.public_research qasper-run --split dev --offset 0 --count 2
.venv-ui/bin/python -m evaluation.public_research qasper-run --split dev --offset 2 --count 8
# Substitute the actual two suite directories printed by the preceding commands:
.venv-ui/bin/python -m evaluation.public_research qasper-merge PATH_TO_FIRST_SUITE PATH_TO_SECOND_SUITE
```

`--offset` skips papers, not questions; each paper contributes its first question. Default budget: 10 main rounds, up to 3 workers, 6 rounds per worker, and a 12,000-character soft compaction threshold. All are configurable (`--steps`, `--workers`, `--worker-steps`, `--budget-chars`); the threshold is neither a token limit nor the model context-window size. The model determines whether to delegate. Model calls incur costs.

The official **Answer F1** measures normalized token overlap, not semantic correctness. The official **Evidence F1** compares exact paragraph texts against reference annotations. Our prediction adapter selects full paragraphs covered by both final-answer citations and recorded complete reads, then supplies the original paragraph strings to the official scorer. Invalid or unread citations select no evidence. This mapping is conservative: truncated reads, references with non-line citation formats, and equally valid but unannotated evidence can be undercounted. Answer conversion strips line citations and an `ANSWER:`/`EVIDENCE:` wrapper. The dataset text includes figure/table captions but not the visual contents, so some questions cannot be adequately answered from this representation. Results from this adapter are useful diagnostic comparisons, not automatically comparable with systems using different answer/evidence extraction or full figures.

`summary.json` contains settings, dataset and scorer hashes, citations, time, token usage and run directories. The per-run `gold.json` is stored outside the agent's document database, and its `predictions.jsonl` is used only by the scorer. The merged report checks hashes, settings, model names, and duplicate questions. Failed runs count as missing predictions in official metrics; check each case's execution status separately. Tiny, nonrandom samples cannot establish overall model quality; use more questions and document the sample method before drawing conclusions.

Official source: https://allenai.org/data/qasper ; dataset v0.3 archives downloaded by `qasper-prepare` from the source URLs in `qasper.py`.

## BrowseComp-Plus — integration preparation

`browsecomp-download`, `browsecomp-index`, and `browsecomp-run` are experimental. The first downloads all seven version-pinned official corpus shards (roughly 1.5–2 GB compressed); the second builds a separate full-corpus SQLite FTS5/BM25 index and stores shard SHA-256s. Install the optional `requirements-public-benchmarks.txt` first. Query Parquet must be supplied using `--query-parquet`, for example the first shard at the version-pinned official Hugging Face dataset. The adapter uses Astrion's loop with a custom local retriever and no subagents; it records but does not grade answers. Official BrowseComp-Plus grading uses Qwen3-32B, which is not included. Differences from official tool and retriever limits prevent treating this as a leaderboard result. The integration has offline fixture coverage only; full-corpus and real-model validation remain pending.

## ScholarQA-CS2 — integration preparation

`scholarqa.execute()` runs the real research loop in an isolated project, with Astrion's own research tools. `asta_solver.py` supplies an optional Inspect solver for the official AstaBench task and scorer, but has not yet been validated against the gated official dataset. This is a custom toolset, not the standard Asta MCP environment. Official scorer expects structured section/citation JSON and uses a separate model service; missing dataset authorization, scoring credentials, or a malformed model response can prevent a valid score. Accept the upstream dataset terms and configure credentials locally; never send tokens to a third-party mirror. Do not call this an official benchmark result until the integration and scorer have actually run successfully.
