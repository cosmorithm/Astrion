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

## QASPER evidence diagnostics and review

After `qasper-merge`, analyze the actual recorded tool responses without another Agent run:

```bash
.venv-ui/bin/python -m evaluation.public_research qasper-analyze PATH_TO_MERGED_SUMMARY --k 5
# Optional: use a separate LLM call as a provisional reviewer. This incurs extra costs.
.venv-ui/bin/python -m evaluation.public_research qasper-analyze PATH_TO_MERGED_SUMMARY --k 5 --model-review
# Set ASTRION_JUDGE_API_KEY / ASTRION_JUDGE_BASE_URL or --judge-model to use an independent provider/model.
# Reuse previously completed reviews only when input, cited source, question, prompt and judge model match:
.venv-ui/bin/python -m evaluation.public_research qasper-analyze PATH_TO_MERGED_SUMMARY \
  --model-review --reuse PATH_TO_PREVIOUS_ANALYSIS_SUMMARY
# Export a human review form; edit reviewer, verdict and reason, preserving the source fields.
.venv-ui/bin/python -m evaluation.public_research qasper-review-template PATH_TO_ANALYSIS_SUMMARY --output review.json
.venv-ui/bin/python -m evaluation.public_research qasper-apply-review PATH_TO_ANALYSIS_SUMMARY review.json
```

The diagnostics are distinct:

- `first_search_recall_at_k_macro`: for each scorable question, count the fraction of a human-annotated evidence set present **in full** in the first `search_knowledge` call's first K returned excerpts. Average across scorable questions. A successful run with no search or a failed first search scores zero for the first attempt; later successful searches do not replace it. An absent, malformed, incomplete, or failed run is unknown.
- `first_search_partial_recall_at_k_macro` is a separate, less strict diagnostic: at least 80 contiguous normalized characters of an annotated paragraph (or the full paragraph if shorter) must appear in an excerpt. A partial hit does not prove that sufficient evidence was retrieved or that the claim is supported.
- `all_search_recall_macro` and `all_search_partial_recall_macro` use the union of all returned search results, so they are **not** Recall@K. `all_observed_recall_macro` additionally includes recorded `read_knowledge` and `read_document` responses; evidence must occur fully in a returned excerpt. This is a conservative trace-based measurement, not a semantic similarity judgment.
- Each score uses the maximum over eligible complete text-only reference annotations, then macro-averages questions. Cases where any annotator says unanswerable, or no complete reference is represented as text (such as missing tables/figures), are excluded and separately counted. An annotation may have multiple valid evidence alternatives beyond those marked in QASPER; exact matching can undercount. Results report the measured denominator and excluded reasons.
- Official Answer F1 and Evidence F1 remain unchanged and are shown separately. The support reviewer assesses answer claims against cited source text, and marks missing/unread citations, missing source, or overlong evidence as `unknown`. A narrow pattern guard also downgrades a supported verdict on an explicit inability/absence claim to `unknown`; other unsupported claims can still escape the guard. It is **provisional**, especially if it uses the same model as the agent, and does not establish semantic correctness. The report records reviewer token usage independently.
- A human review form carries the question, answer, references and cited excerpts. `unknown` never counts as a pass. Applying the review validates its source hash and unchanged evidence fields, requires reasons for decided judgments, and writes a new report without modifying the original run.


For interrupted or failed suites:

```bash
# Recover finalized cases to a new directory; pass the original command's exact options.
.venv-ui/bin/python -m evaluation.public_research qasper-recover PATH_TO_INTERRUPTED_SUITE --offset ORIGINAL_OFFSET
# Once a merged suite has failures, retry those exact offsets if desired, then create
# a clearly labelled separate score that substitutes only successful retries of failed cases:
.venv-ui/bin/python -m evaluation.public_research qasper-apply-retries PATH_TO_ORIGINAL_MERGED_SUMMARY PATH_TO_RETRY_SUITE
```

Each finalized case is written as it finishes; a fatal `SystemExit` from the model client is recorded for that case and the next case is attempted. The recovery command reconstructs selected cases only after checking their IDs, paper text and predictions, leaves the interrupted source untouched, and records that settings were supplied by the caller. It cannot recover an in-flight case's final answer or verify settings that were not saved; retain the original launch command. The separately generated retry report checks the data/evaluator hashes, paper annotations, budgets and model name; it includes failed and retry usage separately in the total and preserves the original report. Never present the with-retry result as a no-retry run.

## Compare main-agent round budgets

To compare 10 / 6 / 3, 15 / 6 / 3 and 20 / 6 / 3 on exactly the same 10 development questions:

```bash
# Run from astrion-agent/. These commands call the model and incur costs.
.venv-ui/bin/python -m evaluation.public_research qasper-run --split dev --offset 0 --count 10 --steps 10 --worker-steps 6 --workers 3 --budget-chars 12000
.venv-ui/bin/python -m evaluation.public_research qasper-run --split dev --offset 0 --count 10 --steps 15 --worker-steps 6 --workers 3 --budget-chars 12000
.venv-ui/bin/python -m evaluation.public_research qasper-run --split dev --offset 0 --count 10 --steps 20 --worker-steps 6 --workers 3 --budget-chars 12000
# Run qasper-merge on each suite separately to obtain its merged summary, then:
.venv-ui/bin/python -m evaluation.public_research qasper-compare PATH_TO_10_SUMMARY PATH_TO_15_SUMMARY PATH_TO_20_SUMMARY
```

`qasper-compare` verifies equal case IDs, dataset/scorer hashes, model name and all budgets except the main-agent limit; it rejects retry reports by default and duplicate limits. To create a separately labelled successful-results comparison after retries, pass `--allow-retries`; the report records retry counts and includes their usage. It reports the official scores on all cases (including missing predictions), plus paired scores on questions answered in **every** group. Failed cases stay visible. Root steps come from the terminal events; absent or damaged events remain unknown. Token counts, actual worker creation, retrieval diagnostics, and durations are reported separately. Summed per-case durations are not wall-clock time, especially for concurrently run groups. Source Git revisions and dirty states are included, but a dirty worktree or a mutable model service means the exact implementation or provider behavior is not fully pinned. One run per condition and 10 nonrandom cases are only an exploratory comparison; repeat trials and use a larger fixed sample before drawing robust conclusions. The 12,000-character soft compaction threshold matches the earlier pilot and is not the model's context window or a token cap.
