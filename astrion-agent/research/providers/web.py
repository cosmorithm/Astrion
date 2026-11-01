"""Fetch, extract and persist public content; extraction never certifies paper full text."""

from pathlib import Path
from urllib.parse import urlsplit
from .http import PublicHTTP, ServiceError
from research.storage.sources import Sources


class WebReader:
    def __init__(self, store, http=None):
        self.store = store
        self.http = http or PublicHTTP()

    def read(self, project, url):
        self.store.project(project)
        if len(url) > 4000:
            raise ValueError("url_too_long")
        response = self.http.fetch(url)
        snapshots = Sources(self.store.db)
        meta = snapshots.save(project, response)
        previous = snapshots.previous_document(project, response["url"], meta["snapshot_sha256"])
        kind = response["content_type"]
        data = response["body"]
        extra = meta | {"paper_full_text_verified": False}
        if previous:
            doc = previous
        else:
            name = "source_" + meta["snapshot_sha256"][:20]
            if kind == "application/pdf" or data.startswith(b"%PDF-"):
                name += ".pdf"
                extra["content_kind"] = "pdf_text"
            elif kind in {"text/html", "application/xhtml+xml"}:
                try:
                    import trafilatura

                    text = trafilatura.extract(data, include_tables=True, include_comments=False)
                except Exception as exc:
                    raise ServiceError("extraction_failed", type(exc).__name__) from exc
                if not text or len(text.strip()) < 30:
                    raise ServiceError(
                        "no_readable_body",
                        "page may require login, JavaScript, or contain no article text",
                    )
                if len(text) > 900_000:
                    raise ServiceError("extracted_text_too_large", "select a smaller source")
                data = text.encode()
                name += ".md"
                abstract_page = urlsplit(response["url"]).hostname in {
                    "arxiv.org",
                    "www.arxiv.org",
                } and urlsplit(response["url"]).path.startswith("/abs/")
                extra.update(
                    content_kind="paper_abstract" if abstract_page else "web_page",
                    warnings=["page_body_only", "paper_full_text_not_verified"],
                )
            elif kind in {"text/plain", "text/markdown"}:
                name += ".txt"
                extra["content_kind"] = "web_page"
            else:
                raise ServiceError("unsupported_media", "only PDF, HTML or UTF-8 text")
            # Same body at another URL needs a distinct name/source observation.
            import hashlib

            name = (
                name.rsplit(".", 1)[0]
                + "_"
                + hashlib.sha256(response["url"].encode()).hexdigest()[:8]
                + "."
                + name.rsplit(".", 1)[1]
            )
            doc = self.store.import_document(project, name, data, response["url"], extra=extra)
        snapshots.link(meta["snapshot_id"], doc)
        observation = self.store.document_store.read_part(project, doc, limit=4000)
        return {
            "document_id": doc,
            "snapshot": meta,
            "results": [observation],
            "full_text_read": False,
            "limitation": "This is the fetched page/PDF extraction. Read all needed parts and inspect warnings before claiming full-text coverage.",
        }
