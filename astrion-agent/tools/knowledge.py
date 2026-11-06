"""Knowledge tools: one declaration set and per-agent bound observations."""

from research.citations import covers_range
from .contracts import spec, S, I, dump

SEARCH_DOCS = spec(
    "search_knowledge",
    "检索当前项目资料；中英文语义与关键词融合，offset 分页；降级状态见 retrieval。",
    {
        "query": S,
        "offset": I,
        "limit": I,
        "mode": {"type": "string", "enum": ["hybrid", "keyword", "semantic"]},
    },
    ["query"],
)
READ_DOC = spec(
    "read_knowledge",
    "按行读原文，每次最多返回 80 行和 6000 字符；范围过大时按 next_page 接续，截断时先按 continuation 接续。",
    {"document_id": I, "start_line": I, "end_line": I},
    ["document_id", "start_line", "end_line"],
)
READ_PART = spec(
    "read_document",
    "按页码或章节 ID 阅读；或按行范围；字符 offset 接续到 next_offset=null。保留质量警告。",
    {
        "document_id": I,
        "page": I,
        "section": I,
        "start_line": I,
        "end_line": I,
        "offset": I,
        "limit": I,
    },
    ["document_id"],
)
LIST_DOCS = spec(
    "list_documents",
    "列出当前项目资料和文档 ID。limit 为1–20；按输出预算分页可能少于 limit，必须用 next_offset 接续，null 表示结束。",
    {"offset": I, "limit": I},
)
OUTLINE = spec(
    "document_outline",
    "浏览文档章节或页面目录。offset>=0，limit 为 1–50（默认30）；用 next_offset 接续，null 表示结束。",
    {"document_id": I, "offset": I, "limit": I},
    ["document_id"],
)
SEARCH_MEM = spec("search_memory", "查询全局对话记忆和当前项目有效记忆。", {"query": S}, ["query"])
KNOWLEDGE_SPECS = [LIST_DOCS, OUTLINE, SEARCH_DOCS, READ_DOC, READ_PART, SEARCH_MEM]


class KnowledgeTools:
    def __init__(self, store, project):
        self.store = store
        self.project = project
        self.seen = set()

    def observe(self, data):
        results = data["results"]
        ranges = {(r["document_id"], r["start_line"], r["end_line"]) for r in results}
        count = sum(not covers_range(self.seen, *span) for span in ranges)
        self.seen.update(
            (r["document_id"], r["start_line"], r["end_line"])
            for r in results
            if not r["truncated"]
        )
        return dump(data | {"new_ranges": count})

    def search_knowledge(self, query, offset=0, limit=5, mode="hybrid"):
        return self.observe(
            self.store.search(self.project, query, offset=offset, limit=limit, mode=mode)
        )

    def read_knowledge(self, document_id, start_line, end_line):
        # Keep each observation bounded; large selections require subsequent calls.
        if any(type(n) is not int for n in (document_id, start_line, end_line)) or start_line < 1 or end_line < start_line:
            raise ValueError("invalid_line_range: start_line 和 end_line 必须是有效的正整数范围")
        page_end = min(end_line, start_line + 79)
        result = self.store.read_document(self.project, document_id, start_line, page_end)
        if result.get("continuation"):
            result["continuation"]["document_id"] = document_id
            # This line range has not been completely observed yet.
            result["next_line"] = None
        next_start = result["end_line"] + 1
        next_page = None
        if next_start <= min(end_line, result["total_lines"]):
            next_page = {"tool": "read_knowledge", "document_id": document_id,
                         "start_line": next_start, "end_line": end_line}
        return self.observe({
            "results": [result],
            "requested_range": {"start_line": start_line, "end_line": end_line},
            "selection_complete": next_page is None and not result["truncated"] and end_line <= result["total_lines"],
            "requested_end_outside_document": end_line > result["total_lines"],
            "next_page": next_page,
            "pagination_note": "若结果截断，先使用 continuation 读完当前页，再使用 next_page。"
            if result["truncated"] else "使用 next_page 继续读取请求范围。" if next_page else None,
        })

    def read_document(
        self, document_id, page=0, section=0, start_line=1, end_line=0, offset=0, limit=4000
    ):
        return self.observe(
            {
                "results": [
                    self.store.document_store.read_part(
                        self.project,
                        document_id,
                        page=page,
                        section=section,
                        start_line=start_line,
                        end_line=end_line,
                        offset=offset,
                        limit=limit,
                    )
                ]
            }
        )

    def document_outline(self, document_id, offset=0, limit=30):
        return dump(self.store.document_store.outline(self.project, document_id, offset, limit))

    def list_documents(self, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 20:
            raise ValueError(
                "invalid_pagination: offset>=0; limit must be 1..20, follow next_offset"
            )
        rows = self.store.document_store.listing(self.project, offset, limit + 1)
        selected = []
        total = self.store.document_store.count(self.project)
        for row in rows[:limit]:
            candidate = {
                "documents": selected + [row],
                "next_offset": offset + len(selected) + 1,
                "total": total,
            }
            if selected and len(dump(candidate)) > 2600:
                break
            selected.append(row)
        return dump(
            {
                "documents": selected,
                "next_offset": offset + len(selected) if offset + len(selected) < total else None,
                "total": total,
            }
        )

    def search_memory(self, query):
        return dump({"results": self.store.search_memories(self.project, query)})

    def implementations(self):
        return {s.name: getattr(self, s.name) for s in KNOWLEDGE_SPECS}
