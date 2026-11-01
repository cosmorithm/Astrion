"""Deterministic reciprocal-rank fusion; exact matching plus optional multilingual vectors."""

import hashlib
import json
import math
import re
from .embeddings import from_env


def terms(text):
    result = set()
    for token in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower()):
        if "\u3400" <= token[0] <= "\u9fff":
            result.update(token)
            result.update(token[i : i + 2] for i in range(len(token) - 1))
        else:
            result.add(token)
    return result


def chunks(row, repository):
    lines = row["content"].splitlines()
    for first in range(0, len(lines), 10):
        text = "\n".join(lines[first : first + 10])
        # Smaller semantic chunks avoid silently embedding only a long paragraph prefix.
        for offset in range(0, max(1, len(text)), 1200):
            part = text[offset : offset + 1200]
            yield repository.observation(
                row,
                first + 1,
                min(first + 10, len(lines)),
                part,
                truncated=offset > 0 or len(text) > 1200,
            ) | {"chunk_offset": offset, "chunk_chars": len(text)}


def search(store, project, query, *, offset=0, limit=5, mode="hybrid", encoder=None):
    store.project(project)
    if (
        not query.strip()
        or len(query) > 1000
        or offset < 0
        or not 1 <= limit <= 20
        or mode not in {"hybrid", "keyword", "semantic"}
    ):
        raise ValueError("invalid_search_parameters")
    wanted = terms(query)
    if not wanted:
        raise ValueError("检索词不能为空")
    items = []
    for row in store.db.execute(
        "SELECT * FROM documents WHERE project_id=? ORDER BY id", (project,)
    ):
        items.extend(chunks(dict(row), store.document_store))
    lexical = {
        i: len(wanted & terms(x["excerpt"])) + 8 * (query.casefold() in x["excerpt"].casefold())
        for i, x in enumerate(items)
    }
    ranks = {i: 0.0 for i in range(len(items))}
    diagnostics = {
        "mode": mode,
        "semantic": "disabled",
        "ranking": "RRF k=60; lexical weight 1; semantic weight 1; stable document/line order",
    }
    if mode != "semantic":
        for rank, i in enumerate(
            sorted((i for i in lexical if lexical[i]), key=lambda i: (-lexical[i], i)), 1
        ):
            ranks[i] += 1 / (60 + rank)
    if mode != "keyword" and items:
        try:
            encoder = encoder or from_env(store.path.parent / "models")
            if encoder is None:
                diagnostics["semantic"] = "not_configured"
            else:
                vectors = []
                missing = []
                for i, x in enumerate(items):
                    key = hashlib.sha256((x["sha256"] + x["excerpt"]).encode()).hexdigest()
                    row = store.db.execute(
                        "SELECT vector FROM embedding_cache WHERE document_id=? AND backend=? AND chunk_key=?",
                        (x["document_id"], encoder.name, key),
                    ).fetchone()
                    vectors.append(json.loads(row[0]) if row else None)
                    if not row:
                        missing.append((i, key))
                for start in range(0, len(missing), 32):
                    batch = missing[start : start + 32]
                    encoded = encoder.encode([items[i]["excerpt"] for i, _ in batch])
                    if len(encoded) != len(batch):
                        raise ValueError("embedding_count_mismatch")
                    with store.db:
                        for (i, key), v in zip(batch, encoded):
                            vectors[i] = v
                            store.db.execute(
                                "INSERT OR REPLACE INTO embedding_cache VALUES (?,?,?,?)",
                                (
                                    items[i]["document_id"],
                                    encoder.name,
                                    key,
                                    json.dumps(v, allow_nan=False),
                                ),
                            )
                q = encoder.encode([query], query=True)[0]

                def cosine(v):
                    if len(q) != len(v):
                        raise ValueError("embedding_dimension_mismatch")
                    return sum(a * b for a, b in zip(q, v)) / (
                        math.sqrt(sum(a * a for a in q) * sum(b * b for b in v)) or 1
                    )

                scores = {i: cosine(v) for i, v in enumerate(vectors)}
                for rank, i in enumerate(sorted(scores, key=lambda i: (-scores[i], i)), 1):
                    ranks[i] += 1 / (60 + rank)
                diagnostics.update(semantic="available", backend=encoder.name)
        except Exception as exc:
            diagnostics.update(semantic="unavailable", error=type(exc).__name__)
    ordered = sorted((i for i in ranks if ranks[i]), key=lambda i: (-ranks[i], i))
    seen = set()
    results = []
    for i in ordered:
        x = items[i]
        key = (x["sha256"], x["start_line"], x["chunk_offset"], x["excerpt"])
        if key in seen:
            continue
        seen.add(key)
        results.append(x | {"score": lexical[i], "rank_score": round(ranks[i], 8)})
    return {
        "results": results[offset : offset + limit],
        "total": len(results),
        "next_offset": offset + limit if offset + limit < len(results) else None,
        "retrieval": diagnostics,
    }
