import struct
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


def rec_table(rows: int, cols: int) -> bytes:
    return bytes(4) + struct.pack("<HH", rows, cols) + bytes(8)


def rec_cell(row: int, col: int) -> bytes:
    return bytes(8) + struct.pack("<HHHH", col, row, 1, 1) + bytes(8)


def rec_text(text: str) -> bytes:
    return text.encode("utf-16le")


class TableCellEscapeTests(unittest.TestCase):
    def test_pipe_is_escaped_once_and_angle_brackets_are_entities(self) -> None:
        cell = hwp2md.escape_table_cell("가|나 <신 설> 다_라" + chr(10) + "마")
        self.assertEqual(cell, "가" + chr(92) + "|나 &lt;신 설&gt; 다" + chr(92) + "_라<br>마")


class SingleUnitControlTests(unittest.TestCase):
    def test_fixed_width_space_does_not_swallow_following_text(self) -> None:
        # 0x1F(고정폭 빈칸)는 1유닛 문자다. 확장 제어문자로 오인해 뒤 글자를 건너뛰면 본문이 사라진다.
        raw = "아래" + chr(31) + "표시" + chr(31) + "부동산에" + chr(31) + "관하여"
        text = hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True)
        self.assertEqual(text, "아래 표시 부동산에 관하여")

    def test_hyphen_control_becomes_hyphen(self) -> None:
        raw = "가" + chr(24) + "나"
        self.assertEqual(hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True), "가-나")


class ListMarkerTests(unittest.TestCase):
    def kind(self, text: str):
        blocks: list[hwp2md.Block] = []
        hwp2md.append_text_block(blocks, text, "none", is_first=False)
        return blocks[0].type, blocks[0].text

    def test_parenthesized_date_is_not_a_list(self) -> None:
        self.assertEqual(self.kind("(15. 10. 31. 현재)"), ("paragraph", "(15. 10. 31. 현재)"))

    def test_paired_parenthesis_number_is_still_a_list(self) -> None:
        self.assertEqual(self.kind("(2) 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신합니다."), ("ordered_list", "위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신합니다."))


class DigitOnlyTableCellTests(unittest.TestCase):
    def test_digit_only_cells_are_not_dropped_as_page_numbers(self) -> None:
        # 숫자만 있는 표 칸은 쪽 번호가 아니라 데이터다. 거의 빈 격자 표에서 전부 사라지면 안 된다.
        ctrl = hwp2md.HWP_TABLE_CTRL_ID + bytes(8)
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, ctrl),
            (hwp2md.HWP_TABLE_TAG, 2, rec_table(3, 3)),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("1")),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(2, 2)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("2")),
        ]
        blocks, _ = hwp2md.parse_hwp_table_records(records, 0, "none", True, False)
        self.assertEqual([b.text for b in blocks], ["1", "2"])


class TableCaptionTests(unittest.TestCase):
    def test_caption_before_table_record_is_kept(self) -> None:
        # CTRL(tbl) 바로 뒤, TABLE 레코드보다 앞에 오는 문단 리스트는 캡션이며 셀이 아니다.
        ctrl = hwp2md.HWP_TABLE_CTRL_ID + bytes(8)
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, ctrl),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, bytes(16)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("표 처리결과 캡션")),
            (hwp2md.HWP_TABLE_TAG, 2, rec_table(2, 2)),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("구분")),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 1)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("2014년")),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(1, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("조정률")),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(1, 1)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("19%")),
        ]
        blocks, _ = hwp2md.parse_hwp_table_records(records, 0, "none", True, False)
        self.assertEqual(blocks[0].text, "표 처리결과 캡션")
        self.assertEqual(blocks[-1].type, "table")
        self.assertEqual([c.text for c in blocks[-1].cells], ["구분", "2014년", "조정률", "19%"])


class TableCellBreakTests(unittest.TestCase):
    def test_existing_line_break_markup_is_kept_while_text_is_escaped(self) -> None:
        # 셀 텍스트에는 줄바꿈을 뜻하는 <br>가 이미 들어 있다. 이것까지 &lt;br&gt;로 바꾸면 안 된다.
        self.assertEqual(hwp2md.escape_table_cell("전년대비<br>증감률"), "전년대비<br>증감률")
        self.assertEqual(hwp2md.escape_table_cell("<신 설><br>가|나"), "&lt;신 설&gt;<br>가" + chr(92) + "|나")


class NestedTableTests(unittest.TestCase):
    def test_nested_table_does_not_overwrite_outer_table_cells(self) -> None:
        # 바깥 표 1칸 안에 2x2 표가 있다. 안쪽 셀 위치(0,0)가 바깥 셀과 겹쳐도 바깥 문단이 사라지면 안 된다.
        ctrl = hwp2md.HWP_TABLE_CTRL_ID + bytes(8)
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, ctrl),
            (hwp2md.HWP_TABLE_TAG, 2, rec_table(1, 1)),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("바깥 문단")),
            (hwp2md.HWP_CTRL_HEADER_TAG, 3, ctrl),
            (hwp2md.HWP_TABLE_TAG, 4, rec_table(2, 2)),
            (hwp2md.HWP_TABLE_CELL_TAG, 4, rec_cell(0, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 5, rec_text("안쪽 가")),
            (hwp2md.HWP_TABLE_CELL_TAG, 4, rec_cell(0, 1)),
            (hwp2md.HWP_PARA_TEXT_TAG, 5, rec_text("안쪽 나")),
            (hwp2md.HWP_TABLE_CELL_TAG, 4, rec_cell(1, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 5, rec_text("안쪽 다")),
            (hwp2md.HWP_TABLE_CELL_TAG, 4, rec_cell(1, 1)),
            (hwp2md.HWP_PARA_TEXT_TAG, 5, rec_text("안쪽 라")),
        ]
        blocks, next_idx = hwp2md.parse_hwp_table_records(records, 0, "none", True, False)
        pieces = [b.text or "" for b in blocks]
        pieces += [cell for b in blocks for row in (b.rows or []) for cell in row]
        text = " ".join(pieces)
        for expected in ("바깥 문단", "안쪽 가", "안쪽 나", "안쪽 다", "안쪽 라"):
            self.assertIn(expected, text)
        self.assertEqual(next_idx, len(records))


if __name__ == "__main__":
    unittest.main()
