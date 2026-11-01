import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from research.providers.http import PublicHTTP, ServiceError
from research.providers.literature import Crossref
from research.providers.web import WebReader
from research.store import Store
from tools.external import ExternalTools, EXTERNAL_SPECS
from tooling import ToolRegistry, ToolCall


class ExternalTests(unittest.TestCase):
    def test_metadata_paging_dedup_and_abstract_not_fulltext(self):
        item = {
            "DOI": "10.1234/Test",
            "title": ["Paper"],
            "author": [{"given": "A", "family": "B"}],
            "published": {"date-parts": [[2024]]},
            "abstract": "<jats:p>abstract</jats:p>",
            "reference": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}],
        }

        class HTTP:
            def json(self, url):
                return (
                    {"message": {"items": [item, item], "next-cursor": "next", "total-results": 2}}
                    if "query." in url
                    else {"message": item}
                )

        provider = Crossref(HTTP())
        result = provider.search("query", limit=2)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["next_cursor"], "next")
        metadata = provider.metadata("10.1234/Test", limit=1)
        self.assertEqual(metadata["authors"], ["A B"])
        self.assertEqual(metadata["next_reference_offset"], 1)
        self.assertFalse(metadata["full_text_read"])
        self.assertEqual(metadata["abstract"], "abstract")

    def test_private_addresses_and_credentials_are_blocked(self):
        client = PublicHTTP()
        for url in [
            "file:///etc/passwd",
            "http://user:secret@example.com",
            "http://127.0.0.1",
            "http://[::1]",
            "http://169.254.169.254",
        ]:
            with self.subTest(url=url), self.assertRaises(ServiceError):
                client.fetch(url)

    def test_timeout_and_rate_limit_are_typed_and_bounded(self):
        client = PublicHTTP()
        with patch.object(
            client, "fetch", side_effect=ServiceError("rate_limited", "wait", "60")
        ) as fetch:
            with self.assertRaises(ServiceError):
                client.json("https://example.org")
            self.assertEqual(fetch.call_count, 1)
        with patch.object(client, "fetch", side_effect=ServiceError("timeout", "request deadline")):
            with self.assertRaisesRegex(ServiceError, "timeout"):
                client.json("https://example.org")

    def test_web_snapshot_import_dedup_and_project_isolation(self):
        body = b"<html><body><article><h1>Research</h1><p>Retrieved passages provide external evidence for generated answers. Sources must support claims and preserve their original locations.</p></article></body></html>"

        class HTTP:
            def fetch(self, url):
                return {"url": url, "content_type": "text/html", "body": body}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "db")
            self.addCleanup(store.close)
            a = store.create_project("A")
            b = store.create_project("B")
            reader = WebReader(store, HTTP())
            first = reader.read(a, "https://example.org/article")
            second = reader.read(a, "https://example.org/article")
            self.assertEqual(first["document_id"], second["document_id"])
            self.assertEqual(first["results"][0]["content_kind"], "web_page")
            self.assertFalse(first["full_text_read"])
            self.assertEqual(
                store.db.execute("select body from source_snapshots limit 1").fetchone()[0], body
            )
            with self.assertRaises(ValueError):
                store.read_document(b, first["document_id"], 1, 1)

    def test_access_failure_is_tool_error_not_successful_fulltext(self):
        class Web:
            def read(self, *args):
                raise ServiceError("access_denied", "HTTP 403")

        tools = ExternalTools(None, 1, web=Web())
        registry = ToolRegistry(EXTERNAL_SPECS, tools.implementations())
        result = registry.execute(
            ToolCall("a", "read_webpage", json.dumps({"url": "https://example.org"}))
        )
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.error["code"], "access_denied")


class ExternalApiTests(unittest.TestCase):
    def test_api_uses_same_tools_and_rejects_unknown_project(self):
        from fastapi.testclient import TestClient
        from research import api

        with tempfile.TemporaryDirectory() as directory, patch.object(
            api, "DB_PATH", Path(directory) / "db"
        ), TestClient(api.app) as client:
            project = client.post("/api/projects", json={"name": "external"}).json()["id"]
            with patch(
                "tools.external.Crossref.search",
                return_value={
                    "results": [{"title": "paper", "doi": "10.1234/a", "full_text_read": False}],
                    "next_cursor": None,
                },
            ):
                response = client.get(f"/api/projects/{project}/literature?query=test")
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["results"][0]["full_text_read"])
                self.assertEqual(
                    client.get("/api/projects/999/literature?query=test").status_code, 400
                )
            with patch(
                "tools.external.WebReader.read",
                side_effect=ServiceError("access_denied", "HTTP 403"),
            ):
                self.assertIn(
                    "access_denied",
                    client.post(
                        f"/api/projects/{project}/sources/import",
                        json={"url": "https://example.org"},
                    ).json()["detail"],
                )


class DoiTests(unittest.TestCase):
    def test_datacite_fallback_preserves_abstract_status(self):
        from research.providers.doi import LiteratureCatalog

        class Cross:
            def metadata(self, *args):
                raise ServiceError("http_error", "HTTP 404")

        class HTTP:
            def json(self, url):
                return {
                    "data": {
                        "attributes": {
                            "titles": [{"title": "RAG"}],
                            "creators": [{"name": "Author"}],
                            "publicationYear": 2020,
                            "doi": "10.48550/arXiv.2005.11401",
                            "descriptions": [
                                {"descriptionType": "Abstract", "description": "Abstract only"}
                            ],
                        }
                    }
                }

        result = LiteratureCatalog(Cross(), HTTP()).metadata("10.48550/arXiv.2005.11401")
        self.assertEqual(result["source"], "DataCite")
        self.assertFalse(result["full_text_read"])
        self.assertEqual(result["abstract"], "Abstract only")
