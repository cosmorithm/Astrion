import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from research import api
from .test_library import FIXTURES


class LibraryApiTests(unittest.TestCase):
    def test_upload_browse_search_read_and_project_isolation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            api, "DB_PATH", Path(directory) / "db"
        ), TestClient(api.app) as client:
            a = client.post("/api/projects", json={"name": "A"}).json()["id"]
            b = client.post("/api/projects", json={"name": "B"}).json()["id"]
            upload = client.post(
                f"/api/projects/{a}/documents/upload",
                files={
                    "file": (
                        "paper.pdf",
                        (FIXTURES / "reading_fixture.pdf").read_bytes(),
                        "application/pdf",
                    )
                },
            )
            self.assertEqual(upload.status_code, 200)
            doc = upload.json()["id"]
            self.assertEqual(
                client.get(f"/api/projects/{a}/documents/{doc}/outline").json()["page_count"], 3
            )
            self.assertIn(
                "Retrieval",
                client.get(f"/api/projects/{a}/documents/{doc}/read?page=1").json()["excerpt"],
            )
            self.assertEqual(
                client.get(f"/api/projects/{b}/documents/{doc}/read?page=1").status_code, 400
            )
            self.assertTrue(
                client.get(f"/api/projects/{a}/search?query=Retrieval&mode=keyword").json()[
                    "results"
                ]
            )
            bad = client.post(
                f"/api/projects/{a}/documents/upload",
                files={"file": ("../bad.pdf", b"bad", "application/pdf")},
            )
            self.assertEqual(bad.status_code, 400)
