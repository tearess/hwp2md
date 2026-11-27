"""2단계 P1 개선(형식·제목 추론·그림 위치·경고·입출력 오류·배치 리포트) 시험."""
import contextlib
import io
import json
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import hwp2md

PNG_300 = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + (300).to_bytes(4, "big")
    + (300).to_bytes(4, "big")
    + b"\x08\x02\x00\x00\x00"
)


def make_png_file(path: Path, size: int = 300) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + size.to_bytes(4, "big")
        + size.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def write_hwpx_with_picture(path: Path, refs: tuple[str, ...] = ("image1",)) -> None:
    body = "".join(f'<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="{ref}"/></hp:pic></hp:run></hp:p>' for ref in refs)
    section = (
        '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"'
        ' xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
        ' xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
        "<hp:p><hp:run><hp:t>첫 문단</hp:t></hp:run></hp:p>"
        f"{body}"
        "<hp:p><hp:run><hp:t>끝 문단</hp:t></hp:run></hp:p>"
        "</hs:sec>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/section0.xml", section)
        zf.writestr("BinData/image1.png", PNG_300)


HWPML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<HWPML><BODY><SECTION><P><CHAR>{title}</CHAR></P><P><CHAR>본문 내용입니다.</CHAR></P></SECTION></BODY></HWPML>
"""


def write_hwpml(path: Path, title: str) -> None:
    path.write_text(HWPML_TEMPLATE.format(title=title), encoding="utf-8")


def run_main(argv):
    """CLI 진행 출력은 시험 결과를 가리지 않도록 모아 둔다."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
        code = hwp2md.main(argv)
    return code, stderr.getvalue()


def render(blocks, assets=None, assets_dir=None, **options):
    report = hwp2md.build_report(Path("문서.hwp"), "hwp", blocks, assets or [], [], [])
    opts = hwp2md.ConvertOptions(**options)
    output_path = Path("문서.md") if assets_dir is None else assets_dir.parent / "문서.md"
    directory = assets_dir if assets_dir is not None else Path(".")
    return hwp2md.render_markdown(Path("문서.hwp"), blocks, assets or [], report, opts, output_path, directory)


class MarkdownFormatTests(unittest.TestCase):
    """P1-2: --format gfm/commonmark/obsidian이 실제로 다른 출력을 만든다."""

    def test_default_format_is_gfm(self):
        self.assertEqual(hwp2md.ConvertOptions().markdown_format, "gfm")
        self.assertEqual(hwp2md.markdown_profile(None).name, "gfm")

    def test_unknown_format_falls_back_to_gfm(self):
        self.assertEqual(hwp2md.markdown_profile("kramdown").name, "gfm")

    def test_obsidian_uses_wiki_embeds_and_gfm_uses_markdown_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets_dir = root / "문서.assets"
            assets_dir.mkdir()
            make_png_file(assets_dir / "image-001.png")
            blocks = [hwp2md.Block(type="image_anchor")]
            asset = hwp2md.Asset(source="BinData/BIN0001.png", output="image-001.png", kind="image", bytes=100)

            gfm = render(blocks, [asset], assets_dir, frontmatter="none", markdown_format="gfm")
            obsidian = render(blocks, [asset], assets_dir, frontmatter="none", markdown_format="obsidian")

        self.assertIn("![image-001](문서.assets/image-001.png)", gfm)
        self.assertIn("![[문서.assets/image-001.png]]", obsidian)
        self.assertNotIn("![image-001](", obsidian)

    def test_commonmark_renders_tables_as_html(self):
        rows = [["항목", "금액"], ["보증금", "1,000"]]
        self.assertEqual(hwp2md.render_table(rows, "hybrid", None, "html")[0], "<table>")
        self.assertTrue(hwp2md.render_table(rows, "hybrid", None, "pipe")[0].startswith("| "))

        blocks = [hwp2md.Block(type="table", rows=rows)]
        with tempfile.TemporaryDirectory():
            gfm = render(blocks, frontmatter="none", markdown_format="gfm")
            commonmark = render(blocks, frontmatter="none", markdown_format="commonmark")

        self.assertIn("| 항목 | 금액 |", gfm)
        self.assertNotIn("<table>", gfm)
        self.assertIn("<table>", commonmark)
        self.assertNotIn("| 항목 |", commonmark)

    def test_commonmark_uses_numbered_footnotes(self):
        blocks = [hwp2md.Block(type="paragraph", text="본문"), hwp2md.Block(type="footnote", text="각주 내용")]
        gfm = render(blocks, frontmatter="none", markdown_format="gfm")
        commonmark = render(blocks, frontmatter="none", markdown_format="commonmark")

        self.assertIn("[^1]", gfm)
        self.assertIn("[^1]: 각주 내용", gfm)
        self.assertIn("[1]", commonmark)
        self.assertIn("1. 각주 내용", commonmark)
        self.assertNotIn("[^1]", commonmark)

    def test_obsidian_converts_inline_math_and_addstags(self):
        blocks = [
            hwp2md.Block(type="heading", text="문서 제목", level=1),
            hwp2md.Block(type="paragraph", text="수식 \\(a^2+b^2\\)"),
        ]
        obsidian = render(blocks, frontmatter="yaml", markdown_format="obsidian")
        gfm = render(blocks, frontmatter="yaml", markdown_format="gfm")

        self.assertIn("$a^2+b^2$", obsidian)
        self.assertIn("tags:", obsidian)
        self.assertIn('aliases:\n  - "문서 제목"', obsidian)
        self.assertIn("\\(a^2+b^2\\)", gfm)
        self.assertNotIn("tags:", gfm)

    def test_cli_format_option_reaches_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_hwpx_with_picture(root / "그림.hwpx")
            gfm_md = root / "gfm.md"
            obsidian_md = root / "obsidian.md"
            self.assertEqual(run_main([str(root / "그림.hwpx"), "-o", str(gfm_md), "--format", "gfm"])[0], 0)
            self.assertEqual(run_main([str(root / "그림.hwpx"), "-o", str(obsidian_md), "--format", "obsidian"])[0], 0)

            gfm_text = gfm_md.read_text(encoding="utf-8")
            obsidian_text = obsidian_md.read_text(encoding="utf-8")

        self.assertNotEqual(gfm_text, obsidian_text)
        self.assertIn("![", gfm_text)
        self.assertIn("![[", obsidian_text)
        self.assertIn("markdown_format: obsidian", obsidian_text)


class HeadingInferenceTests(unittest.TestCase):
    """P1-3·P1-4: 법령 각 호 위계와 첫 줄 H1 오탐."""

    def _blocks(self, *lines):
        blocks: list[hwp2md.Block] = []
        for line in lines:
            hwp2md.append_text_block(blocks, line, "font", is_first=not hwp2md.has_content_blocks(blocks))
        return blocks

    def test_numbered_clause_run_becomes_one_ordered_list(self):
        blocks = self._blocks(
            "제15조(개인정보의 수집ㆍ이용) ① 개인정보처리자는 다음 각 호의 어느 하나에 해당하는 경우에는 개인정보를 수집할 수 있다.",
            "1. 정보주체의 동의를 받은 경우",
            "2. 법률에 특별한 규정이 있거나 법령상 의무를 준수하기 위하여 불가피한 경우",
            "3. 공공기관이 법령 등에서 정하는 소관 업무의 수행을 위하여 불가피한 경우",
        )
        hwp2md.normalize_heading_structure(blocks)

        self.assertEqual([block.type for block in blocks], ["paragraph", "ordered_list", "ordered_list", "ordered_list"])
        self.assertEqual([block.number for block in blocks[1:]], [1, 2, 3])
        self.assertEqual(blocks[1].text, "정보주체의 동의를 받은 경우")
        self.assertTrue(all(block.level is None for block in blocks[1:]))

        markdown = render(blocks, frontmatter="none")
        self.assertNotIn("## 1. 정보주체", markdown)
        self.assertIn("1. 정보주체의 동의를 받은 경우\n2. 법률에 특별한 규정이", markdown)

    def test_numbered_sections_separated_by_body_stay_headings(self):
        blocks = self._blocks(
            "1. 질의 배경",
            "귀 공사는 위 사업과 관련하여 질의하였습니다.",
            "2. 검토 의견",
            "검토 의견은 다음과 같습니다.",
        )
        hwp2md.normalize_heading_structure(blocks)

        self.assertEqual([block.type for block in blocks], ["heading", "paragraph", "heading", "paragraph"])
        self.assertEqual([block.level for block in blocks if block.type == "heading"], [2, 2])

    def test_single_clause_after_legal_article_is_not_heading(self):
        blocks = self._blocks(
            "제5조(보상) ① 손실을 보상한다. 다음 각 호와 같다.",
            "1. 토지 및 그 정착물",
        )
        hwp2md.normalize_heading_structure(blocks)
        self.assertEqual(blocks[1].type, "ordered_list")
        self.assertEqual(blocks[1].number, 1)

    def test_first_line_date_is_not_h1(self):
        for line in ["2026. 11.", "2026년 1월 일", "2026. 1. 14."]:
            self.assertIsNone(hwp2md.infer_heading_level(line, "font", is_first=True), line)

    def test_letterhead_lines_are_not_h1(self):
        lines = [
            "전 화 : (02) 599-8969 법무법인 케이씨엘 팩시밀리 : (02) 599-8960",
            "수 신 : 서울주택도시공사",
            "의뢰번호 : 2026-11",
            "서울주택도시개발공사",
            "서울 서초구 서초중앙로26길17 동신빌딩(서초동) 3층,5층",
            "E-mail : help@example.com",
        ]
        for line in lines:
            self.assertIsNone(hwp2md.infer_heading_level(line, "font", is_first=True), line)
            self.assertTrue(hwp2md.is_letterhead_or_metadata_line(line), line)

    def test_real_title_as_first_line_stays_h1(self):
        self.assertEqual(hwp2md.infer_heading_level("상계마들단지 재정비사업의 건", "font", is_first=True), 1)

    def test_explicit_title_label_is_h1_without_label(self):
        self.assertEqual(hwp2md.parse_explicit_heading("제 목 : 법률자문의뢰에 대한 회신"), (1, "법률자문의뢰에 대한 회신"))
        # HWP 본문은 공백이 줄어 콜론이 사라지기도 한다.
        self.assertEqual(hwp2md.parse_explicit_heading("제 목 수용재결 일부 실효에 따른 후속조치 관련 법률의견"), (1, "수용재결 일부 실효에 따른 후속조치 관련 법률의견"))
        self.assertIsNone(hwp2md.parse_explicit_heading("제 목적 및 범위"))

    def test_letterhead_first_block_does_not_become_h1_in_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "레터.hwpml"
            source.write_text(
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?><HWPML><BODY><SECTION>"
                "<P><CHAR>2026. 1. 14.</CHAR></P>"
                "<P><CHAR>수 신 : 서울주택도시공사</CHAR></P>"
                "<P><CHAR>제 목 : 법률자문의뢰에 대한 회신</CHAR></P>"
                "<P><CHAR>본문 내용입니다.</CHAR></P>"
                "</SECTION></BODY></HWPML>",
                encoding="utf-8",
            )
            result = hwp2md.convert_file(source, root / "out.md", None, None, hwp2md.ConvertOptions(frontmatter="none"))

        self.assertIn("# 법률자문의뢰에 대한 회신", result.markdown)
        self.assertNotIn("# 2026. 1. 14.", result.markdown)
        self.assertNotIn("# 수 신", result.markdown)
        self.assertEqual(sum(1 for line in result.markdown.splitlines() if line.startswith("# ")), 1)


class PicturePlacementTests(unittest.TestCase):
    """P1-5: 그림 개체의 BinData 인덱스로 본문 위치를 되찾는다."""

    def test_picture_record_bin_id_is_read_from_offset_71(self):
        payload = bytearray(91)
        struct.pack_into("<H", payload, 71, 3)
        self.assertEqual(hwp2md.parse_hwp_picture_bin_id(bytes(payload)), 3)
        self.assertIsNone(hwp2md.parse_hwp_picture_bin_id(bytes(bytearray(91))))
        self.assertIsNone(hwp2md.parse_hwp_picture_bin_id(b"short"))

    def test_shape_picture_bin_ids_follow_nested_records(self):
        records = [
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, b" osg"),
            (hwp2md.HWP_TABLE_CELL_TAG + 5, 2, bytes(196)),  # SHAPE_COMPONENT
            (85, 3, self._picture_payload(2)),
            (66, 0, b""),
            (hwp2md.HWP_CTRL_HEADER_TAG, 1, b" osg"),
            (85, 2, self._picture_payload(1)),
            (66, 0, b""),
        ]
        self.assertEqual(hwp2md.hwp_shape_picture_bin_ids(records, 0), [2])
        self.assertEqual(hwp2md.hwp_shape_picture_bin_ids(records, 4), [1])

    def _picture_payload(self, bin_id: int) -> bytes:
        payload = bytearray(91)
        struct.pack_into("<H", payload, 71, bin_id)
        return bytes(payload)

    def test_anchor_target_matches_hwp_index_and_hwpx_name(self):
        hwp_asset = hwp2md.Asset(source="BinData/BIN0002.png", output="image-002.png", index=2)
        hwpx_asset = hwp2md.Asset(source="BinData/image1.JPEG", output="image-001.jpeg")
        self.assertTrue(hwp2md.anchor_target_matches(hwp_asset, "bin:2"))
        self.assertFalse(hwp2md.anchor_target_matches(hwp_asset, "bin:1"))
        self.assertTrue(hwp2md.anchor_target_matches(hwpx_asset, "image1"))
        self.assertFalse(hwp2md.anchor_target_matches(hwpx_asset, "image2"))

    def test_targeted_anchor_places_the_matching_asset_and_keeps_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "문서.assets"
            assets_dir.mkdir()
            make_png_file(assets_dir / "image-001.png")
            make_png_file(assets_dir / "image-002.png")
            assets = [
                hwp2md.Asset(source="BinData/BIN0001.png", output="image-001.png", kind="image", index=1, width=300, height=300),
                hwp2md.Asset(source="BinData/BIN0002.png", output="image-002.png", kind="image", index=2, width=300, height=300),
            ]
            blocks = [
                hwp2md.Block(type="paragraph", text="도입"),
                hwp2md.Block(type="image_anchor", target="bin:2"),
                hwp2md.Block(type="paragraph", text="중간"),
                hwp2md.Block(type="image_anchor", target="bin:1"),
                hwp2md.Block(type="image_anchor", target="bin:2"),
            ]
            markdown = render(blocks, assets, assets_dir, frontmatter="none")

        order = [line for line in markdown.splitlines() if line.startswith("![")]
        self.assertEqual(len(order), 3)
        self.assertIn("image-002", order[0])
        self.assertIn("image-001", order[1])
        self.assertIn("image-002", order[2])
        self.assertNotIn("## 첨부 자산", markdown)

    def test_hwpx_picture_becomes_anchor_and_is_placed_in_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "그림.hwpx"
            write_hwpx_with_picture(source, refs=("image1", "image1"))
            result = hwp2md.convert_file(source, root / "그림.md", None, None, hwp2md.ConvertOptions(frontmatter="none"))

        self.assertEqual([block.target for block in result.blocks if block.type == "image_anchor"], ["image1", "image1"])
        self.assertIn("첫 문단", result.markdown)
        self.assertEqual(result.markdown.count("![image-001]"), 2)
        self.assertNotIn("## 첨부 자산", result.markdown)
        self.assertEqual([asset.output for asset in result.assets], ["image-001.png"])

    def test_placement_summary_counts_control_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "문서.assets"
            assets_dir.mkdir()
            make_png_file(assets_dir / "image-001.png")
            make_png_file(assets_dir / "image-002.png")
            assets = [
                hwp2md.Asset(source="BinData/BIN0001.png", output="image-001.png", kind="image", index=1, width=300, height=300),
                hwp2md.Asset(source="BinData/BIN0002.png", output="image-002.png", kind="image", index=2, width=300, height=300),
            ]
            blocks = [hwp2md.Block(type="image_anchor", target="bin:1")]
            summary = hwp2md.summarize_asset_placements(assets, blocks, assets_dir)

            warnings: list[hwp2md.WarningItem] = []
            losses: list[hwp2md.LossItem] = []
            hwp2md.add_asset_position_warnings(assets, blocks, assets_dir, warnings, losses)

        self.assertEqual((summary.control, summary.approximated, summary.excluded), (1, 1, 0))
        codes = [item.code for item in warnings]
        self.assertIn("ASSET_POSITION_FROM_CONTROL", codes)
        self.assertIn("ASSET_INLINE_POSITION_APPROXIMATED", [item.code for item in losses])

    def test_small_decorative_asset_is_reported_as_excluded_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "문서.assets"
            assets_dir.mkdir()
            make_png_file(assets_dir / "image-001.png", size=40)
            assets = [hwp2md.Asset(source="BinData/BIN0001.png", output="image-001.png", kind="image", index=1, width=40, height=40)]
            blocks = [hwp2md.Block(type="image_anchor", target="bin:1")]
            warnings: list[hwp2md.WarningItem] = []
            losses: list[hwp2md.LossItem] = []
            summary = hwp2md.summarize_asset_placements(assets, blocks, assets_dir)
            hwp2md.add_asset_position_warnings(assets, blocks, assets_dir, warnings, losses)

        self.assertEqual(summary.excluded, 1)
        self.assertEqual(summary.approximated, 0)
        self.assertIn("ASSET_EXCLUDED_FROM_BODY", [item.code for item in warnings])
        self.assertNotIn("ASSET_INLINE_POSITION_APPROXIMATED", [item.code for item in losses])


class ControlWarningTests(unittest.TestCase):
    """P1-6: 이미 처리하는 control을 UNHANDLED로 보고하지 않는다."""

    def test_hyperlinks_and_auto_numbers_are_not_unhandled(self):
        warnings: list[hwp2md.WarningItem] = []
        losses: list[hwp2md.LossItem] = []
        hwp2md.add_hwp_control_warnings({b"onta": 3, b"klh%": 2}, warnings, losses)

        self.assertNotIn("UNHANDLED_HWP_CONTROLS", [item.code for item in warnings])
        self.assertEqual(warnings, [])

    def test_genuinely_unhandled_control_is_still_reported(self):
        warnings: list[hwp2md.WarningItem] = []
        losses: list[hwp2md.LossItem] = []
        hwp2md.add_hwp_control_warnings({b"knu%": 20, b"onta": 1}, warnings, losses)

        unhandled = [item for item in warnings if item.code == "UNHANDLED_HWP_CONTROLS"]
        self.assertEqual(len(unhandled), 1)
        self.assertIn("knu%", unhandled[0].message)
        self.assertNotIn("onta", unhandled[0].message)


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root는 권한 오류를 재현할 수 없습니다")
class IoErrorTests(unittest.TestCase):
    """P1-7: 파일 접근 오류는 traceback 대신 한 줄 진단과 종료 코드로 끝난다."""

    def test_unreadable_input_prints_message_and_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "잠긴.hwp"
            source.write_bytes(b"not really hwp")
            source.chmod(0o000)
            try:
                code, message = run_main([str(source), "-o", str(root / "out.md")])
            finally:
                source.chmod(0o644)

        self.assertEqual(code, 2)
        self.assertIn("입력 파일을 읽을 수 없습니다", message)
        self.assertIn("권한", message)
        self.assertNotIn("Traceback", message)

    def test_unwritable_output_prints_message_and_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_hwpml(root / "문서.hml", "제목")
            locked = root / "잠긴폴더"
            locked.mkdir()
            locked.chmod(0o555)
            try:
                code, message = run_main([str(root / "문서.hml"), "-o", str(locked / "out.md")])
            finally:
                locked.chmod(0o755)

        self.assertEqual(code, 2)
        self.assertIn("쓸 수 없습니다", message)
        self.assertNotIn("Traceback", message)

    def test_missing_input_is_a_usage_error(self):
        code, message = run_main(["/없는/파일.hwp"])

        self.assertEqual(code, 1)
        self.assertIn("입력 파일을 찾을 수 없습니다", message)

    def test_invalid_option_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--format", "kramdown", "문서.hwp"])

        self.assertEqual(ctx.exception.code, 1)


class BatchReportTests(unittest.TestCase):
    """P1-8: 배치에서 --report를 줘도 문서별 리포트가 서로 덮어쓰지 않는다."""

    def test_batch_report_keeps_one_report_per_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_hwpml(root / "가.hml", "첫 문서")
            write_hwpml(root / "나.hml", "둘째 문서")
            out_dir = root / "out"
            summary_path = root / "shared.json"

            code, _ = run_main([
                "--batch", str(root / "*.hml"),
                "-o", str(out_dir),
                "--report", str(summary_path),
            ])

            self.assertEqual(code, 0)
            first = json.loads((out_dir / "가.report.json").read_text(encoding="utf-8"))
            second = json.loads((out_dir / "나.report.json").read_text(encoding="utf-8"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertIn("가.hml", first["source"])
        self.assertIn("나.hml", second["source"])
        self.assertEqual(summary["batch"], {"documents": 2, "ok": 2, "failed": 0})
        self.assertEqual([item["source"].split("/")[-1] for item in summary["documents"]], ["가.hml", "나.hml"])
        self.assertTrue(all(item["ok"] for item in summary["documents"]))

    def test_single_file_report_option_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_hwpml(root / "문서.hml", "제목")
            report_path = root / "리포트.json"

            code, _ = run_main([str(root / "문서.hml"), "-o", str(root / "문서.md"), "--report", str(report_path)])

            self.assertEqual(code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["format"], "hwpml")
        self.assertIn("문서.hml", report["source"])

    def test_batch_summary_records_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_hwpml(root / "정상.hml", "제목")
            (root / "깨짐.hwpx").write_bytes(b"not a zip")
            summary_path = root / "shared.json"

            code, _ = run_main(["--batch", str(root / "*.*"), "-o", str(root / "out"), "--report", str(summary_path)])
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertEqual(summary["batch"]["failed"], 1)
        failed = [item for item in summary["documents"] if not item["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("error", failed[0])


if __name__ == "__main__":
    unittest.main()
