"""引用位置检查；不等价于语义支持评判。"""

import json
import re


def _excerpt_lines(found):
    excerpt = found.get("excerpt")
    if not isinstance(excerpt, str):
        return None
    lines = excerpt.split("\n")
    expected = found["end_line"] - found["start_line"] + 1
    if len(lines) == expected + 1 and lines[-1] == "":
        lines.pop()  # A page ending on a line boundary includes its newline.
    return lines if len(lines) == expected else None


def covers_range(observed, document_id, start, end):
    """多个相邻/重叠片段可覆盖一个引用，但不可跨过未读行。"""
    if start > end:
        return False
    cursor = start
    for doc, seen_start, seen_end in sorted(observed):
        if doc != document_id or seen_end < cursor:
            continue
        if seen_start > cursor:
            return False
        cursor = max(cursor, seen_end + 1)
        if cursor > end:
            return True
    return False


def _observations(trace):
    """Return actual successful source results, with their actor and tool."""
    observations = []
    for item in trace:
        event = item["event"]
        payload = event.get("payload", {})
        if (
            event["type"] != "tool.call.finished"
            or payload.get("name")
            not in {"search_knowledge", "read_knowledge", "read_document", "read_webpage"}
            or payload.get("outcome") != "success"
        ):
            continue
        try:
            results = json.loads(payload["content"]).get("results", [])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not isinstance(results, list):
            continue
        for found in results:
            if not isinstance(found, dict) or found.get("truncated") is not False:
                continue
            doc, start, end = (found.get(k) for k in ("document_id", "start_line", "end_line"))
            if any(type(x) is not int for x in (doc, start, end)) or start < 1 or end < start:
                continue
            # Metadata or a directory range alone is not an observed passage.
            # Require the actual returned text to match the advertised lines.
            if _excerpt_lines(found) is None:
                continue
            observations.append((found, item.get("role"), item.get("worker_id"),
                                 payload["name"], event.get("step")))
    return observations


def observed_ranges(trace):
    """Only successful, untruncated source reads count as observed locations."""
    return sorted({(x[0]["document_id"], x[0]["start_line"], x[0]["end_line"])
                   for x in _observations(trace)})


def audit_citations(answer, trace, prior_ranges=()):
    """引用位置检查覆盖继承的真实阅读记录；不判断语义支持。"""
    citations = re.findall(r"\[D(\d+):L(\d+)(?:-L(\d+))?\]", answer or "")
    observations = _observations(trace)
    inherited = [tuple(r) for r in prior_ranges]
    observed = inherited + [(x[0]["document_id"], x[0]["start_line"], x[0]["end_line"])
                            for x in observations]
    checks = []
    for doc, start, end in citations:
        doc, start, end = int(doc), int(start), int(end or start)
        evidence = []
        for found, role, worker_id, tool, step in observations:
            if start > end:
                break
            if found["document_id"] != doc or found["end_line"] < start or found["start_line"] > end:
                continue
            if not isinstance(found.get("excerpt"), str):
                continue
            first, last = max(start, found["start_line"]), min(end, found["end_line"])
            lines = _excerpt_lines(found)
            text = "\n".join(lines[first-found["start_line"]:last-found["start_line"]+1])
            evidence.append({
                "document_id": doc, "start_line": first, "end_line": last,
                "name": found.get("name"), "sha256": found.get("sha256"),
                "pages": [p.get("page") for p in (found.get("pages") or []) if isinstance(p, dict)],
                "role": role, "worker_id": worker_id, "tool": tool, "step": step,
                "excerpt": text[:800], "display_truncated": len(text) > 800,
            })
            if len(evidence) == 2:
                break
        checks.append(
            {
                "citation": f"[D{doc}:L{start}-L{end}]",
                "in_retrieved_range": covers_range(observed, doc, start, end),
                "inherited_range": covers_range(inherited, doc, start, end),
                "evidence": evidence,
            }
        )
    return {
        "status": (
            "no_citations"
            if not checks
            else (
                "locations_found"
                if all(c["in_retrieved_range"] for c in checks)
                else "invalid_locations"
            )
        ),
        "checks": checks,
        "semantic_support_checked": False,
    }
