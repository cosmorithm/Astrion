"""Crossref metadata provider. Search summaries and abstracts are not full text."""

import html
import os
import re
from urllib.parse import quote, urlencode
from .http import PublicHTTP, ServiceError


def plain(value):
    return html.unescape(re.sub("<[^>]+>", " ", value or "")).strip()


def normalize(item):
    dates = item.get("published", item.get("issued", {})).get("date-parts", [[]])[0]
    return {
        "title": (item.get("title") or ["Untitled"])[0],
        "authors": [
            " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")
            for a in item.get("author", [])
        ][:30],
        "year": dates[0] if dates else None,
        "doi": item.get("DOI", "").lower(),
        "url": item.get("URL"),
        "source": "Crossref",
        "publication_type": item.get("type"),
        "content_kind": "metadata",
        "abstract": plain(item.get("abstract"))[:4000],
        "abstract_truncated": len(plain(item.get("abstract"))) > 4000,
        "full_text_read": False,
        "links": [
            {"url": x.get("URL"), "content_type": x.get("content-type")}
            for x in item.get("link", [])
        ][:5],
    }


class Crossref:
    def __init__(self, http=None, base=None):
        self.http = http or PublicHTTP(timeout=25, max_bytes=5_000_000)
        self.base = (
            base or os.environ.get("ASTRION_LITERATURE_BASE_URL") or "https://api.crossref.org"
        ).rstrip("/")

    def url(self, path, params):
        email = os.environ.get("ASTRION_LITERATURE_EMAIL")
        if email:
            params = {**params, "mailto": email}
        return self.base + path + "?" + urlencode(params)

    def search(self, query, cursor="*", limit=5):
        if not query.strip() or len(query) > 1000 or not 1 <= limit <= 20 or len(cursor) > 4000:
            raise ValueError("invalid_literature_query")
        data = self.http.json(
            self.url(
                "/works",
                {
                    "query.bibliographic": query,
                    "cursor": cursor,
                    "rows": limit,
                    "sort": "score",
                    "order": "desc",
                },
            )
        )
        try:
            message = data["message"]
            items = message["items"]
        except (TypeError, KeyError) as exc:
            raise ServiceError("invalid_response", "missing Crossref works") from exc
        unique = {}
        for item in items:
            row = normalize(item)
            key = row["doi"] or (row["title"].casefold(), row["year"])
            unique.setdefault(key, row)
        return {
            "results": list(unique.values()),
            "next_cursor": message.get("next-cursor") if len(items) == limit else None,
            "total": message.get("total-results"),
            "source": "Crossref",
            "full_text_read": False,
        }

    def metadata(self, doi, reference_offset=0, limit=20):
        doi = re.sub(r"^https?://(?:dx\.)?doi.org/", "", doi.strip(), flags=re.I)
        if (
            not re.fullmatch(r"10\.\d{4,9}/\S{1,200}", doi)
            or reference_offset < 0
            or not 1 <= limit <= 30
        ):
            raise ValueError("invalid_doi_or_pagination")
        data = self.http.json(self.url("/works/" + quote(doi, safe=""), {}))
        try:
            item = data["message"]
            result = normalize(item)
            references = item.get("reference", [])
        except (TypeError, KeyError) as exc:
            raise ServiceError("invalid_response", "missing DOI metadata") from exc
        refs = [
            {k: r[k] for k in ("DOI", "author", "year", "article-title", "unstructured") if k in r}
            for r in references[reference_offset : reference_offset + limit]
        ]
        return result | {
            "references": refs,
            "reference_count": len(references),
            "next_reference_offset": (
                reference_offset + limit if reference_offset + limit < len(references) else None
            ),
        }
