import struct
import tempfile
import unittest
from xml.etree import ElementTree as ET
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


class CellFootnoteTests(unittest.TestCase):
    def test_footnote_inside_table_cell_is_inlined_without_dangling_marker(self) -> None:
        ctrl = hwp2md.HWP_TABLE_CTRL_ID + bytes(8)
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, ctrl),
            (hwp2md.HWP_TABLE_TAG, 2, rec_table(1, 2)),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 0)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("금액")),
            (hwp2md.HWP_CTRL_HEADER_TAG, 3, b"  nf" + bytes(8)),
            (hwp2md.HWP_TABLE_CELL_TAG, 4, bytes(16)),
            (hwp2md.HWP_PARA_TEXT_TAG, 5, rec_text("각주 본문")),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 1)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("비고")),
        ]
        blocks, next_idx = hwp2md.parse_hwp_table_records(records, 0, "none", True, False)
        text = " ".join(b.text or "" for b in blocks)
        self.assertIn("금액 (각주: 각주 본문)", text)
        self.assertIn("비고", text)
        self.assertNotIn("[^", text)
        self.assertEqual(next_idx, len(records))

    def test_count_cell_footnotes(self) -> None:
        cell = hwp2md.TableCell(row=0, col=0, text="금액 (각주: 본문)")
        self.assertEqual(hwp2md.count_cell_footnotes([hwp2md.Block(type="table", rows=[["x"]], cells=[cell])]), 1)


class FootnoteAndHeaderTests(unittest.TestCase):
    def test_control_id_sets_use_little_endian_ids(self) -> None:
        self.assertIn(b"  nf", hwp2md.HWP_FOOTNOTE_CTRL_IDS)
        self.assertNotIn(b"toof", hwp2md.HWP_FOOTNOTE_CTRL_IDS)
        self.assertIn(b"toof", hwp2md.HWP_HEADER_FOOTER_CTRL_IDS)

    def test_footnote_text_is_extracted_from_nested_paragraphs(self) -> None:
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, b"  nf" + bytes(8)),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, bytes(16)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("각주 첫 문단")),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("각주 둘째 문단")),
            (hwp2md.HWP_PARA_TEXT_TAG, 1, rec_text("다시 본문")),
        ]
        text, next_idx = hwp2md.extract_hwp_footnote(records, 0)
        self.assertEqual(text, "각주 첫 문단 각주 둘째 문단")
        self.assertEqual(next_idx, 4)

    def test_skip_nested_records_stops_at_same_level(self) -> None:
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, b"toof" + bytes(8)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("쪽 꼬리말")),
            (hwp2md.HWP_PARA_TEXT_TAG, 1, rec_text("본문")),
        ]
        self.assertEqual(hwp2md.skip_nested_records(records, 0), 2)


HP_NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def hwpx_blocks(xml: str) -> list[hwp2md.Block]:
    root = ET.fromstring(xml)
    blocks: list[hwp2md.Block] = []
    hwp2md.walk_hwpx_blocks(root, blocks, "none")
    return blocks


class HwpxTableTests(unittest.TestCase):
    def tc(self, col: int, row: int, text: str, colspan: int = 1, rowspan: int = 1) -> str:
        paragraphs = "".join(f"<hp:p><hp:run><hp:t>{line}</hp:t></hp:run></hp:p>" for line in text.split("|")) if text else "<hp:p><hp:run/></hp:p>"
        return (
            f"<hp:tc><hp:subList>{paragraphs}</hp:subList>"
            f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="{colspan}" rowSpan="{rowspan}"/></hp:tc>'
        )

    def test_table_inside_paragraph_is_restored_with_spans_and_cell_lines(self) -> None:
        xml = (
            f"<hp:sec {HP_NS}><hp:p><hp:run><hp:t>표 앞 문장입니다</hp:t></hp:run><hp:run><hp:tbl>"
            f"<hp:tr>{self.tc(0, 0, '구분')}{self.tc(1, 0, '내용', colspan=2)}</hp:tr>"
            f"<hp:tr>{self.tc(0, 1, '가')}{self.tc(1, 1, '첫째|둘째')}{self.tc(2, 1, '다')}</hp:tr>"
            f"</hp:tbl></hp:run></hp:p></hp:sec>"
        )
        blocks = hwpx_blocks(xml)
        self.assertEqual(blocks[0].text, "표 앞 문장입니다")
        table = blocks[-1]
        self.assertEqual(table.type, "table")
        self.assertEqual(len(table.cells), 5)
        self.assertEqual(table.cells[1].colspan, 2)
        self.assertEqual(table.rows[1][1], "첫째<br>둘째")

    def test_paragraph_text_is_not_glued_with_table_text(self) -> None:
        xml = (
            f"<hp:sec {HP_NS}><hp:p><hp:run><hp:tbl>"
            f"<hp:tr>{self.tc(0, 0, '구분')}{self.tc(1, 0, '회사명')}</hp:tr>"
            f"<hp:tr>{self.tc(0, 1, '대표자')}{self.tc(1, 1, '주소')}</hp:tr>"
            f"</hp:tbl></hp:run></hp:p></hp:sec>"
        )
        blocks = hwpx_blocks(xml)
        self.assertTrue(all(b.type != "heading" for b in blocks))
        self.assertEqual([b.type for b in blocks], ["table"])


class CorruptFileTests(unittest.TestCase):
    def test_corrupt_ole_header_becomes_conversion_error(self) -> None:
        # OLE 시그니처는 맞지만 섹터 크기 값이 터무니없는 파일: olefile 내부 ValueError가 그대로 새어 나오면 안 된다.
        header = bytearray(512)
        header[0:8] = hwp2md.HWP_CFB_SIGNATURE
        header[26:28] = struct.pack("<H", 3)
        header[28:30] = struct.pack("<H", 0xFFFE)
        header[30:32] = struct.pack("<H", 0xFFFF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corrupt.hwp"
            path.write_bytes(bytes(header))
            with self.assertRaises(hwp2md.ConversionError):
                hwp2md.convert_file(path, Path(tmp) / "out.md", None, None, hwp2md.ConvertOptions())

    def test_truncated_zip_becomes_conversion_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.hwpx"
            path.write_bytes(b"PK" + bytes(30))
            with self.assertRaises(hwp2md.ConversionError):
                hwp2md.convert_file(path, Path(tmp) / "out.md", None, None, hwp2md.ConvertOptions())


class ContentBlockPerformanceTests(unittest.TestCase):
    def test_has_content_blocks_does_not_copy_the_whole_list(self) -> None:
        import time

        blocks = [hwp2md.Block(type="paragraph", text="가") for _ in range(30000)]
        start = time.perf_counter()
        for _ in range(30000):
            self.assertTrue(hwp2md.has_content_blocks(blocks))
        # 이전에는 호출마다 전체 목록을 복사해서 문단 수의 제곱만큼 걸렸다.
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_metadata_only_blocks_are_not_content(self) -> None:
        self.assertFalse(hwp2md.has_content_blocks([hwp2md.Block(type="metadata", text="{}"), hwp2md.Block(type="page_artifact", text="x")]))
        self.assertTrue(hwp2md.has_content_blocks([hwp2md.Block(type="metadata", text="{}"), hwp2md.Block(type="paragraph", text="가")]))


class BmpConversionTests(unittest.TestCase):
    def png_pixels(self, png: bytes) -> list[bytes]:
        import zlib

        pos = 8
        idat = b""
        width = 0
        while pos < len(png):
            length = struct.unpack(">I", png[pos : pos + 4])[0]
            kind = png[pos + 4 : pos + 8]
            body = png[pos + 8 : pos + 8 + length]
            if kind == b"IHDR":
                width = struct.unpack(">I", body[:4])[0]
            if kind == b"IDAT":
                idat += body
            pos += 12 + length
        raw = zlib.decompress(idat)
        stride = 1 + width * 3
        return [raw[i + 1 : i + stride] for i in range(0, len(raw), stride)]

    def make_bmp(self, bpp: int, rows_bottom_up: list[list[tuple[int, int, int]]]) -> bytes:
        width = len(rows_bottom_up[0])
        height = len(rows_bottom_up)
        pix = bpp // 8
        stride = ((width * bpp + 31) // 32) * 4
        pixels = b""
        for row in rows_bottom_up:
            data = b"".join(bytes([b, g, r]) + (b"\xff" if pix == 4 else b"") for r, g, b in row)
            pixels += data + bytes(stride - len(data))
        header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
        dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bpp, 0, len(pixels), 0, 0, 0, 0)
        return header + dib + pixels

    def test_24_and_32_bit_bmp_are_converted_to_the_same_rgb_pixels(self) -> None:
        # 아래에서 위로 저장되는 BMP: 첫 행이 이미지 맨 아래 줄이다.
        bottom_up = [[(1, 2, 3), (4, 5, 6), (7, 8, 9)], [(10, 11, 12), (13, 14, 15), (16, 17, 18)]]
        expected = [bytes([10, 11, 12, 13, 14, 15, 16, 17, 18]), bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])]
        for bpp in (24, 32):
            png = hwp2md.bmp_to_png(self.make_bmp(bpp, bottom_up))
            self.assertIsNotNone(png)
            self.assertEqual(self.png_pixels(png), expected, f"{bpp}bpp")


class MarkdownSafetyTests(unittest.TestCase):
    def test_leading_hash_and_quote_are_escaped(self) -> None:
        self.assertEqual(hwp2md.escape_md_text("#7. 질문입니다"), chr(92) + "#7. 질문입니다")
        self.assertEqual(hwp2md.escape_md_text("> 인용처럼 보이는 문장"), chr(92) + "> 인용처럼 보이는 문장")
        self.assertEqual(hwp2md.escape_md_text("중간의 # 기호는 그대로"), "중간의 # 기호는 그대로")

    def test_long_clause_sentence_is_not_a_heading(self) -> None:
        long_clause = "제11항 한자음 ‘랴, 려, 례, 료, 류, 리’가 단어의 첫머리에 올 적에는, 두음 법칙에 따라 ‘야, 여, 예, 요, 유, 이’로 적는다."
        self.assertIsNone(hwp2md.infer_heading_level(long_clause, "font"))
        self.assertEqual(hwp2md.infer_heading_level("제3장 총칙", "font"), 2)

    def test_empty_rows_are_dropped_from_gfm_tables(self) -> None:
        out = hwp2md.render_table([["항목", "값"], ["", ""], ["가", "1"]], "gfm")
        self.assertEqual(len(out), 3)
        self.assertNotIn("|  |  |", "\n".join(out))

    def test_empty_file_gets_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.hwp"
            path.write_bytes(b"")
            with self.assertRaises(hwp2md.ConversionError) as ctx:
                hwp2md.detect_format(path)
            self.assertIn("빈 파일", str(ctx.exception))


class EmptyTableRowTests(unittest.TestCase):
    def test_fully_empty_rows_are_not_rendered_in_html_tables(self) -> None:
        cells = [
            hwp2md.TableCell(row=0, col=0, text="가", colspan=1),
            hwp2md.TableCell(row=0, col=1, text="나"),
            hwp2md.TableCell(row=1, col=0, text=""),
            hwp2md.TableCell(row=1, col=1, text=""),
            hwp2md.TableCell(row=2, col=0, text="다"),
            hwp2md.TableCell(row=2, col=1, text="라"),
        ]
        rows = [["가", "나"], ["", ""], ["다", "라"]]
        html_out = "\n".join(hwp2md.render_html_table(rows, cells))
        self.assertEqual(html_out.count("<tr>"), 2)
        self.assertNotIn("<td></td><td></td>", html_out)


class TabControlTests(unittest.TestCase):
    def tab_unit(self, width: int = 0x0FA0) -> str:
        # 탭 인라인 컨트롤: [9, 너비, 0, 0x100, 0, 0, 0, 9]
        return "".join(chr(u) for u in (9, width, 0, 0x100, 0, 0, 0, 9))

    def test_tab_payload_does_not_leak_into_text(self) -> None:
        raw = "담당" + self.tab_unit() + "이름" + self.tab_unit(0x0A8C) + "끝"
        text = hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True)
        self.assertEqual(text, "담당 이름 끝")
        for ch in text:
            self.assertFalse(0x0530 <= ord(ch) <= 0x1AFF, f"stray char U+{ord(ch):04X}")

    def test_plain_tab_without_payload_is_kept(self) -> None:
        self.assertEqual(hwp2md.clean_hwp_decoded_text("가" + chr(9) + "나", skip_extended_controls=True), "가 나")


class SymbolFontTests(unittest.TestCase):
    def test_wingdings_private_use_glyphs_become_standard_symbols(self) -> None:
        raw = "설치" + chr(0xF09E) + "운영 " + chr(0xF53A) + "글 " + chr(0xF0FC)
        self.assertEqual(hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True), "설치·운영 한글 ✓")

    def test_context_verified_private_use_glyphs(self) -> None:
        raw = "속기록" + chr(0xF85E) + "녹음 명칭" + chr(0xF0A0) + "위치 담당( " + chr(0xF028) + ": 229-3610) " + chr(0xF0FE) + "동의"
        self.assertEqual(hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True), "속기록·녹음 명칭·위치 담당( ☎: 229-3610) ☑동의")

    def test_leading_symbol_font_bullet_becomes_list_item(self) -> None:
        blocks: list[hwp2md.Block] = []
        raw = chr(0xF06D) + " 산업체 수요에 의한 맞춤형 교육과정을 운영하기 위한 계약학과 설치 근거를 마련한다."
        text = hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True)
        hwp2md.append_text_block(blocks, text, "none", is_first=False)
        self.assertEqual((blocks[0].type, blocks[0].text), ("bullet_list", "산업체 수요에 의한 맞춤형 교육과정을 운영하기 위한 계약학과 설치 근거를 마련한다."))


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


class EmptyTableCaptionTests(unittest.TestCase):
    def test_caption_text_survives_when_all_cells_are_empty(self) -> None:
        # 칸이 전부 빈 표는 버리지만, 캡션에 글이 있으면 그 글은 남아야 한다.
        ctrl = hwp2md.HWP_TABLE_CTRL_ID + bytes(8)
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, ctrl),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, bytes(16)),
            (hwp2md.HWP_PARA_TEXT_TAG, 3, rec_text("표 위 캡션 문장")),
            (hwp2md.HWP_TABLE_TAG, 2, rec_table(1, 1)),
            (hwp2md.HWP_TABLE_CELL_TAG, 2, rec_cell(0, 0)),
        ]
        blocks, _ = hwp2md.parse_hwp_table_records(records, 0, "none", True, False)
        self.assertEqual([b.text for b in blocks], ["표 위 캡션 문장"])


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
