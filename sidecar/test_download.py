from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from starlette.exceptions import HTTPException
from starlette.requests import Request

import server


def request(paths: list[object], *, owner: bool = True) -> Request:
    body = json.dumps({"paths": paths}).encode()
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"x-openhost-is-owner", b"true")] if owner else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/download",
            "raw_path": b"/api/download",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("capsule.example", 443),
        },
        receive,
    )


def single_request(path: str, *, owner: bool = True) -> Request:
    headers = [(b"x-openhost-is-owner", b"true")] if owner else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": f"/api/download/{path}",
            "raw_path": f"/api/download/{path}".encode(),
            "path_params": {"rel": path},
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("capsule.example", 443),
        }
    )


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.temp_dir.name).resolve()
        (self.content_dir / "posts").mkdir()
        (self.content_dir / "index.gmi").write_text("# Index\n", encoding="utf-8")
        (self.content_dir / "posts" / "one.gmi").write_text("# One\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_downloads_selected_files_as_zip(self) -> None:
        with patch.object(server, "CONTENT_DIR", self.content_dir):
            response = await server.download_files(request(["index.gmi", "posts/one.gmi", "index.gmi"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "application/zip")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="gemini-pages.zip"')
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            self.assertEqual(archive.namelist(), ["index.gmi", "posts/one.gmi"])
            self.assertEqual(archive.read("index.gmi"), b"# Index\n")
            self.assertEqual(archive.read("posts/one.gmi"), b"# One\n")

    async def test_downloads_single_file_without_altering_bytes(self) -> None:
        raw = b"# Raw\r\n\xff\r\n"
        (self.content_dir / "raw.gmi").write_bytes(raw)

        with patch.object(server, "CONTENT_DIR", self.content_dir):
            response = await server.download_file(single_request("raw.gmi"))

        self.assertEqual(response.body, raw)
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="raw.gmi"')

    async def test_rejects_traversal_and_symlinks(self) -> None:
        with patch.object(server, "CONTENT_DIR", self.content_dir):
            with self.assertRaisesRegex(HTTPException, "invalid path"):
                await server.download_files(request(["../outside.gmi"]))

            (self.content_dir / "link.gmi").symlink_to(self.content_dir / "index.gmi")
            with self.assertRaisesRegex(HTTPException, "symlinks are not allowed"):
                await server.download_files(request(["link.gmi"]))

    async def test_requires_owner(self) -> None:
        with self.assertRaisesRegex(HTTPException, "requires an OpenHost session"):
            await server.download_files(request(["index.gmi"], owner=False))

    async def test_rejects_oversized_file_before_reading_it(self) -> None:
        oversized = self.content_dir / "oversized.gmi"
        with oversized.open("wb") as stream:
            stream.truncate(server.MAX_DOWNLOAD_BYTES + 1)

        with patch.object(server, "CONTENT_DIR", self.content_dir):
            with self.assertRaisesRegex(HTTPException, "selected files exceed"):
                await server.download_files(request(["oversized.gmi"]))


if __name__ == "__main__":
    unittest.main()
