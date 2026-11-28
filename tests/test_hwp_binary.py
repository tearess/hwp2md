import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures

import hwp2md


def codes(result: hwp2md.ConvertResult) -> list[str]:
    return [item["code"] for item in result.report["warnings"]]


class FixtureSanityTests(unittest.TestCase):
    """합성 HWP가 정말 OLE/HWP 구조로 읽히는지 먼저 확인한다."""

    def test_fixture_is_ole_and_has_hwp_streams(self) -> None:
        olefile = __import__("olefile")
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.write_hwp(Path(tmp) / "doc.hwp", fixtures.paragraph("본문"))
            self.assertTrue(olefile.isOleFile(str(path)))
            ole = olefile.OleFileIO(str(path))
            try:
                names = ole.listdir()
                self.assertIn(["FileHeader"], names)
                self.assertIn(["BodyText", "Section0"], names)
            finally:
                ole.close()

    def test_fixture_file_header_matches_hwp_signature(self) -> None:
        self.assertTrue(fixtures.file_header().startswith(b"HWP Document File"))
        # 36번째 바이트의 압축 플래그
        self.assertEqual(fixtures.file_header(compressed=True)[36], 0x01)
        self.assertEqual(fixtures.file_header(compressed=False)[36], 0x00)


class HwpRecordStreamTests(unittest.TestCase):
    def test_records_keep_tag_level_and_payload(self) -> None:
        data = fixtures.paragraph("첫 문단") + fixtures.record(10, 1, b"payload")
        records = list(hwp2md.parse_hwp_records(data))
        self.assertEqual([(tag, level) for tag, level, _ in records], [(67, 0), (10, 1)])
        self.assertEqual(records[1][2], b"payload")

    def test_extended_size_record_is_split_at_the_declared_length(self) -> None:
        big = b"x" * 5000
        data = fixtures.record(11, 0, big) + fixtures.record(12, 0, b"tail")
        records = list(hwp2md.parse_hwp_records(data))
        self.assertEqual([(tag, len(payload)) for tag, _, payload in records], [(11, 5000), (12, 4)])

    def test_truncated_stream_reports_remaining_bytes(self) -> None:
        data = fixtures.paragraph("가" * 100) + fixtures.paragraph("나" * 100)
        problems: list[str] = []
        records = list(hwp2md.parse_hwp_records(data[:-10], problems))
        self.assertEqual(len(records), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("잘렸습니다", problems[0])

    def test_trailing_partial_header_is_reported(self) -> None:
        problems: list[str] = []
        records = list(hwp2md.parse_hwp_records(fixtures.paragraph("짧은 글") + b"\x01\x02", problems))
        self.assertEqual(len(records), 1)
        self.assertIn("2바이트", problems[0])

    def test_complete_stream_reports_nothing(self) -> None:
        problems: list[str] = []
        list(hwp2md.parse_hwp_records(fixtures.paragraph("온전한 글"), problems))
        self.assertEqual(problems, [])


class SyntheticHwpConversionTests(unittest.TestCase):
    """실제 .hwp 표본 없이 OLE → Section 레코드 → BinData 경로를 회귀 검증한다."""

    def convert(self, section: bytes, compressed: bool = True, bindata: dict[str, bytes] | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        source = fixtures.write_hwp(root / "문서.hwp", section, compressed=compressed, bindata=bindata)
        return hwp2md.convert_file(source, root / "문서.md", None, None, hwp2md.ConvertOptions())

    def test_paragraph_text_and_heading_are_converted(self) -> None:
        result = self.convert(fixtures.paragraph("첫 문단입니다.") + fixtures.paragraph("두 번째 문단입니다."))
        self.assertIn("첫 문단입니다.", result.markdown)
        self.assertIn("두 번째 문단입니다.", result.markdown)
        self.assertEqual(result.report["format"], "hwp")

    def test_table_records_are_restored_as_markdown_table(self) -> None:
        table = fixtures.table(2, 2, ["항목", "값", "가격", "100원"])
        result = self.convert(fixtures.paragraph("표 앞 문단") + table)
        self.assertIn("| 항목 | 값 |", result.markdown)
        self.assertIn("| 가격 | 100원 |", result.markdown)
        self.assertIn("table", result.report["blockTypes"])

    def test_bindata_picture_is_extracted_and_anchored_in_body(self) -> None:
        section = fixtures.paragraph("<그림 1> 추이") + fixtures.picture(1)
        result = self.convert(section, bindata={"BIN0001": fixtures.png(800, 600)})
        self.assertEqual([asset.output for asset in result.assets], ["image-001.png"])
        self.assertEqual(result.assets[0].index, 1)
        self.assertIn("![", result.markdown)
        self.assertNotIn("## 첨부 자산", result.markdown)
        self.assertIn("ASSET_POSITION_FROM_CONTROL", codes(result))

    def test_uncompressed_section_is_read_without_decompression(self) -> None:
        section = fixtures.paragraph("압축하지 않은 본문")
        result = self.convert(section, compressed=False)
        self.assertIn("압축하지 않은 본문", result.markdown)
        self.assertNotIn("SECTION_DECOMPRESS_FAILED", codes(result))

    def test_compressed_and_uncompressed_sections_produce_same_blocks(self) -> None:
        section = fixtures.paragraph("같은 본문") + fixtures.table(2, 2, ["가", "나", "다", "라"])
        compressed = self.convert(section, compressed=True)
        plain = self.convert(section, compressed=False)
        self.assertEqual(
            [(block.type, block.text) for block in compressed.blocks],
            [(block.type, block.text) for block in plain.blocks],
        )

    def test_truncated_section_is_reported_instead_of_silently_partial(self) -> None:
        # 레코드가 선언한 크기보다 스트림이 짧으면(OLE 섹터 패딩으로도 메울 수 없을 만큼)
        # 조용히 부분 변환하지 않고 SECTION_TRUNCATED를 남긴다.
        section = fixtures.paragraph("가" * 20000) + fixtures.paragraph("잘려 나갈 뒤 문단")
        result = self.convert(section[:20000], compressed=False)
        self.assertIn("SECTION_TRUNCATED", codes(result))

    def test_frontmatter_source_is_relative_to_output_folder(self) -> None:
        result = self.convert(fixtures.paragraph("본문"))
        self.assertIn('source: "문서.hwp"', result.markdown)

    def test_source_path_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixtures.write_hwp(root / "문서.hwp", fixtures.paragraph("본문"))
            absolute = hwp2md.convert_file(source, root / "abs.md", None, None, hwp2md.ConvertOptions(source_path="abs"))
            self.assertIn(f'source: "{source}"', absolute.markdown)
            none = hwp2md.convert_file(source, root / "none.md", None, None, hwp2md.ConvertOptions(source_path="none"))
            self.assertNotIn("source:", none.markdown)


class HwpStreamLimitTests(unittest.TestCase):
    def test_oversized_plain_section_is_refused_before_reading(self) -> None:
        olefile = __import__("olefile")
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.write_hwp(Path(tmp) / "doc.hwp", fixtures.paragraph("본문"))
            ole = olefile.OleFileIO(str(path))
            try:
                with mock.patch.object(hwp2md, "MAX_SECTION_BYTES", 100), self.assertRaises(hwp2md.DecompressLimitError):
                    hwp2md.read_hwp_stream(ole, ["BodyText", "Section0"], compressed=False)
                # 압축된 스트림도 원본 크기를 먼저 확인한다.
                with mock.patch.object(hwp2md, "MAX_SECTION_BYTES", 100), self.assertRaises(hwp2md.DecompressLimitError):
                    hwp2md.read_hwp_stream(ole, ["BodyText", "Section0"], compressed=True)
            finally:
                ole.close()

    def test_oversized_bindata_is_skipped_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixtures.write_hwp(
                root / "doc.hwp",
                fixtures.paragraph("본문") + fixtures.picture(1),
                bindata={"BIN0001": fixtures.png(800, 600)},
            )
            with mock.patch.object(hwp2md, "MAX_ASSET_BYTES", 100):
                result = hwp2md.convert_file(source, root / "out.md", None, None, hwp2md.ConvertOptions())
            self.assertEqual(result.assets, [])
            self.assertIn("ASSET_SKIPPED", codes(result))

    def test_oversized_section_is_skipped_with_its_own_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixtures.write_hwp(root / "doc.hwp", fixtures.paragraph("본문"))
            with mock.patch.object(hwp2md, "MAX_SECTION_BYTES", 100):
                result = hwp2md.convert_file(source, root / "out.md", None, None, hwp2md.ConvertOptions())
            self.assertIn("SECTION_SKIPPED", codes(result))


class TableFlattenWarningTests(unittest.TestCase):
    def test_flattened_table_records_warning_and_loss(self) -> None:
        blocks: list[hwp2md.Block] = []
        flattened: list[str] = []
        cells = [hwp2md.TableCell(row=0, col=0, text="하나"), hwp2md.TableCell(row=0, col=1, text="")]
        hwp2md.finalize_table_blocks(1, 2, cells, [], "font", False, False, flattened=flattened)
        self.assertEqual(flattened, ["1×2"])

        warnings: list[hwp2md.WarningItem] = []
        losses: list[hwp2md.LossItem] = []
        hwp2md.add_table_flatten_warnings(flattened, warnings, losses)
        self.assertEqual([item.code for item in warnings], ["TABLE_FLATTENED"])
        self.assertEqual(losses[0].code, "TABLE_STRUCTURE_FLATTENED")
        self.assertIn("1×2", warnings[0].message)
        self.assertTrue(blocks == [])

    def test_one_by_one_box_is_not_counted_as_loss(self) -> None:
        flattened: list[str] = []
        cells = [hwp2md.TableCell(row=0, col=0, text="상자 안 본문")]
        hwp2md.finalize_table_blocks(1, 1, cells, [], "font", False, False, flattened=flattened)
        self.assertEqual(flattened, [])

    def test_rendered_table_is_not_flattened(self) -> None:
        flattened: list[str] = []
        cells = [
            hwp2md.TableCell(row=0, col=0, text="항목"),
            hwp2md.TableCell(row=0, col=1, text="값"),
            hwp2md.TableCell(row=1, col=0, text="가격"),
            hwp2md.TableCell(row=1, col=1, text="100원"),
        ]
        blocks = hwp2md.finalize_table_blocks(2, 2, cells, [], "font", False, False, flattened=flattened)
        self.assertEqual(flattened, [])
        self.assertEqual([block.type for block in blocks], ["table"])


class HtmlTableGhostRowTests(unittest.TestCase):
    def test_rowspan_only_empty_row_is_not_rendered(self) -> None:
        cells = [
            hwp2md.TableCell(row=0, col=0, text="제목", rowspan=2),
            hwp2md.TableCell(row=0, col=1, text="값"),
            hwp2md.TableCell(row=1, col=0, text="", rowspan=1),
            hwp2md.TableCell(row=1, col=1, text=""),
            hwp2md.TableCell(row=2, col=0, text="아래"),
            hwp2md.TableCell(row=2, col=1, text="행"),
        ]
        html_out = "\n".join(hwp2md.render_html_table([["", ""]] * 3, cells))
        self.assertEqual(html_out.count("<tr>"), 2)
        self.assertNotIn('<tr><td rowspan="2"></td></tr>', html_out)


class BatchPatternTests(unittest.TestCase):
    def test_nested_glob_patterns_are_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "a.hwp").write_bytes(b"x")
            (root / "sub" / "b.hwp").write_bytes(b"x")
            (root / "sub" / "c.hwpx").write_bytes(b"x")
            one_level = hwp2md.expand_batch(str(root / "*" / "*.hwp"))
            self.assertEqual([path.name for path in one_level], ["b.hwp"])
            recursive = hwp2md.expand_batch(str(root / "**" / "*.hwp"))
            self.assertEqual([path.name for path in recursive], ["a.hwp", "b.hwp"])

    def test_plain_path_is_returned_as_is(self) -> None:
        self.assertEqual(hwp2md.expand_batch("문서.hwp"), [Path("문서.hwp")])

    def test_batch_rejects_markdown_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.hwp").write_bytes(b"x")
            code = hwp2md.main(["--batch", str(root / "*.hwp"), "-o", str(root / "out.md")])
            self.assertEqual(code, 1)
            self.assertFalse((root / "out.md").is_dir())


class NoopOptionWarningTests(unittest.TestCase):
    def test_embed_image_mode_prints_notice(self) -> None:
        args = hwp2md.Hwp2MdArgumentParser().parse_args([])
        namespace = mock.Mock(image_mode="embed", equation_mode="latex")
        with mock.patch("builtins.print") as printer:
            hwp2md.warn_about_noop_options(namespace)
        self.assertIn("embed", printer.call_args[0][0])
        self.assertFalse(args is None)

    def test_equation_image_mode_prints_notice(self) -> None:
        namespace = mock.Mock(image_mode="extract", equation_mode="both")
        with mock.patch("builtins.print") as printer:
            hwp2md.warn_about_noop_options(namespace)
        self.assertIn("both", printer.call_args[0][0])

    def test_default_modes_print_nothing(self) -> None:
        namespace = mock.Mock(image_mode="extract", equation_mode="latex")
        with mock.patch("builtins.print") as printer:
            hwp2md.warn_about_noop_options(namespace)
        printer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
