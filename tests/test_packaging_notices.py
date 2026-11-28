"""MIT 라이선스와 배포용 문서 이용 안내가 배포물에 반영됐는지 확인하는 시험(2026-11-28)."""
import contextlib
import io
import unittest
from pathlib import Path

import hwp2md

ROOT = Path(__file__).resolve().parent.parent


class LicenseMetadataTests(unittest.TestCase):
    """P1-10: MIT 라이선스 파일·패키지 메타데이터·README 안내가 같은 내용을 가리킨다."""

    def test_license_file_is_mit(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 tearess", text)
        self.assertIn("THE SOFTWARE IS PROVIDED", text)

    def test_pyproject_declares_mit(self):
        # requires-python이 3.10까지라 tomllib 대신 텍스트로 확인한다.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('license = "MIT"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)

    def test_readme_links_license_and_notice(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[`LICENSE`](LICENSE)", readme)
        self.assertIn("MIT 라이선스", readme)
        self.assertIn("적법하게 열람·변환할 권한", readme)


class DistributionNoticeTests(unittest.TestCase):
    """P1-11: 복호화 동작은 그대로 두고 --help의 이용 범위·책임 안내만 보강했다."""

    def help_text(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            hwp2md.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        return out.getvalue()

    def test_help_states_usage_scope_and_responsibility(self):
        text = self.help_text()
        self.assertIn("적법하게 열람·변환할", text)
        self.assertIn("책임은 사용자에게 있습니다", text)

    def test_help_states_license_and_no_affiliation(self):
        text = self.help_text()
        self.assertIn("라이선스: MIT", text)
        self.assertIn("한글과컴퓨터와 제휴·후원·승인 관계가 없는", text)

    def test_decryption_helpers_are_kept(self):
        # 이용 범위 문구만 더하고 복호화 기능 자체는 건드리지 않았음을 고정한다.
        self.assertTrue(callable(hwp2md.decrypt_hwp_view_text))
        self.assertTrue(callable(hwp2md.hwp_distribution_key))


if __name__ == "__main__":
    unittest.main()
