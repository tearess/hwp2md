import http.client
import io
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures

import webapp

ROOT = Path(__file__).resolve().parents[1]


def multipart_body(files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----hwp2mdtest"
    chunks = []
    for field, filename, data in files:
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class WebAppUnitTests(unittest.TestCase):
    def test_sanitize_upload_filename_keeps_only_basename(self) -> None:
        self.assertEqual(webapp.sanitize_upload_filename("../../secret.hwp"), "secret.hwp")
        self.assertEqual(webapp.sanitize_upload_filename(r"C:\temp\doc.hwpx"), "doc.hwpx")

    def test_html_page_contains_upload_form(self) -> None:
        page = webapp.html_page().decode("utf-8")

        self.assertIn('action="/convert"', page)
        self.assertIn('name="documents"', page)

    def test_html_page_downloads_with_fetch_and_resets_button(self) -> None:
        page = webapp.html_page().decode("utf-8")

        self.assertIn("fetch(form.action", page)
        self.assertIn("saveBlob(await response.blob()", page)
        self.assertIn("buttonDoneText", page)
        self.assertIn('data-max-upload-bytes="', page)

    def test_zip_download_filename_uses_source_name(self) -> None:
        self.assertEqual(webapp.zip_download_filename(["문서.hwp"]), "문서.zip")
        self.assertEqual(webapp.zip_download_filename(["first.hwpx", "second.hwp"]), "first-외1개.zip")

    def test_attachment_content_disposition_supports_utf8_filename(self) -> None:
        header = webapp.attachment_content_disposition("문서.zip")

        self.assertIn('filename="__.zip"', header)
        self.assertIn("filename*=UTF-8''%EB%AC%B8%EC%84%9C.zip", header)

    def test_write_zip_includes_summary_and_outputs(self) -> None:
        root = ROOT / "_unit_webapp_zip"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        try:
            (root / "document.md").write_text("# Title\n", encoding="utf-8")

            data = webapp.write_zip(root, [{"source": "document.hwp"}])
            zip_path = root / "result.zip"
            zip_path.write_bytes(data)

            with zipfile.ZipFile(zip_path) as archive:
                self.assertIn("summary.json", archive.namelist())
                self.assertIn("document.md", archive.namelist())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_write_zip_drops_executable_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "document.md").write_text("# Title\n", encoding="utf-8")
            (root / "document.assets").mkdir()
            (root / "document.assets" / "image-001.html").write_text("<script>alert(1)</script>", encoding="utf-8")
            (root / "document.assets" / "image-002.exe").write_bytes(b"MZ")

            data = webapp.write_zip(root, [{"source": "document.hwp"}])
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                summary = archive.read("summary.json").decode("utf-8")
            self.assertNotIn("document.assets/image-001.html", names)
            self.assertNotIn("document.assets/image-002.exe", names)
            self.assertIn("document.md", names)
            self.assertIn("excludedFiles", summary)
            self.assertIn("image-001.html", summary)


class WebAppHttpTests(unittest.TestCase):
    """실제 HTTP 요청으로 오류 응답·동시성 제한·변환 응답을 확인한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Hwp2MdRequestHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, files: list[tuple[str, str, bytes]]) -> tuple[int, bytes]:
        body, content_type = multipart_body(files)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", "/convert", body=body, headers={"Content-Type": content_type})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_handler_has_socket_timeout(self) -> None:
        self.assertGreater(webapp.Hwp2MdRequestHandler.timeout, 0)

    def test_internal_error_response_hides_exception_text(self) -> None:
        with mock.patch.object(webapp.hwp2md, "convert_file", side_effect=RuntimeError("boom-secret-path")):
            status, raw = self.post([("documents", "문서.hwp", b"data")])
        text = raw.decode("utf-8", errors="replace")
        self.assertEqual(status, 500)
        self.assertNotIn("boom-secret-path", text)
        self.assertNotIn("RuntimeError", text)
        self.assertIn("내부 오류", text)

    def test_unsupported_extension_is_rejected(self) -> None:
        status, raw = self.post([("documents", "문서.txt", b"data")])
        self.assertEqual(status, 400)
        self.assertIn("지원하지 않는 파일", raw.decode("utf-8", errors="replace"))

    def test_busy_server_answers_service_unavailable(self) -> None:
        acquired = 0
        while webapp.CONVERSION_SLOTS.acquire(blocking=False):
            acquired += 1
        try:
            status, raw = self.post([("documents", "문서.hwp", b"data")])
        finally:
            for _ in range(acquired):
                webapp.CONVERSION_SLOTS.release()
        self.assertEqual(status, 503)
        self.assertIn("다른 변환", raw.decode("utf-8", errors="replace"))

    def test_synthetic_hwp_is_converted_to_zip(self) -> None:
        data = fixtures.build_hwp(fixtures.paragraph("웹앱 변환 본문"), compressed=True)
        status, raw = self.post([("documents", "문서.hwp", data)])
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            summary = archive.read("summary.json").decode("utf-8")
        self.assertIn("문서.md", names)
        self.assertIn('"source": "문서.hwp"', summary)


if __name__ == "__main__":
    unittest.main()
