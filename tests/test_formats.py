import tempfile
import unittest
from pathlib import Path

import hwp2md


def make_cells(rows: int, cols: int, text: str) -> list[hwp2md.TableCell]:
    return [hwp2md.TableCell(row=r, col=c, text=text if (r + c) % 3 == 0 else "") for r in range(rows) for c in range(cols)]


class UnsupportedFormatTests(unittest.TestCase):
    def reason(self, head: bytes):
        return hwp2md.unsupported_format_reason(head)

    def test_hwp3_header_is_explained(self) -> None:
        self.assertIn("HWP 3.0", self.reason(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05"))

    def test_drm_header_is_explained(self) -> None:
        self.assertIn("DRM", self.reason(b"\x9b DRMONE\x00\x00\x00\x00\x00\x00\x00\x00"))

    def test_xml_and_bom_xml_are_explained(self) -> None:
        self.assertIn("XML", self.reason(b"<?xml version=\"1.0\""))
        self.assertIn("XML", self.reason(b"\xef\xbb\xbf<?xml version=\"1.0\""))

    def test_unknown_binary_has_no_specific_reason(self) -> None:
        self.assertIsNone(self.reason(b"\x00\x01\x02\x03\x04\x05\x06\x07"))

    def test_detect_format_raises_readable_error_for_hwp3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.hwp"
            path.write_bytes(b"HWP Document File V3.00 " + b"\x00" * 64)
            with self.assertRaises(hwp2md.ConversionError) as ctx:
                hwp2md.detect_format(path)
            self.assertIn("HWP 3.0", str(ctx.exception))


class PageArtifactTableTests(unittest.TestCase):
    FOOTER = "협조자 시행 접수 전화 서울시 www.i-sh.co.kr"

    def test_short_approval_table_is_artifact(self) -> None:
        cells = make_cells(5, 16, self.FOOTER)
        grid = hwp2md.build_hwp_table_grid(5, 16, cells)
        self.assertTrue(hwp2md.is_page_artifact_table(5, 16, cells, grid))

    def test_tall_form_table_is_kept_even_with_footer_keywords(self) -> None:
        # 신청서 서식은 "접수번호/전화번호" 같은 키워드를 갖지만 본문이므로 버리면 안 된다.
        cells = make_cells(24, 17, self.FOOTER)
        grid = hwp2md.build_hwp_table_grid(24, 17, cells)
        self.assertFalse(hwp2md.is_page_artifact_table(24, 17, cells, grid))


if __name__ == "__main__":
    unittest.main()
