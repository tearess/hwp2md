import unittest
import shutil
import zipfile
from pathlib import Path

import webapp


ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
