import unittest
from pathlib import Path

import hwp2md


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_170508 = ROOT / "170508(조간) 16년말 외국인이 보유한 국내 토지는 233제곱키로미터 전 국토의 0.2퍼센트(토지정책과).hwp"


class Hwp2MdUnitTests(unittest.TestCase):
    def test_source_stem_preserves_original_name(self) -> None:
        path = Path("170508(조간) 원본 파일명.hwp")
        self.assertEqual(hwp2md.source_stem(path), "170508(조간) 원본 파일명")

    def test_press_release_metadata_from_cells(self) -> None:
        cells = [
            hwp2md.TableCell(row=1, col=2, text="보 도 자 료"),
            hwp2md.TableCell(row=2, col=2, text="배포일시"),
            hwp2md.TableCell(row=2, col=3, text="2017. 5. 4(목)\n총 7매(본문4)"),
            hwp2md.TableCell(row=3, col=0, text="담당\n부서"),
            hwp2md.TableCell(row=3, col=1, text="토지정책과"),
            hwp2md.TableCell(row=3, col=2, text="담 당 자"),
            hwp2md.TableCell(row=3, col=3, text="∙과장 김상석\n∙☎ (044)201-3398, 3400"),
            hwp2md.TableCell(row=4, col=0, text="보 도 일 시"),
            hwp2md.TableCell(row=4, col=2, text="2017년 5월 8일(월) 조간부터 보도"),
        ]

        metadata = hwp2md.parse_press_release_metadata(cells)

        self.assertEqual(metadata["document_type"], "보도자료")
        self.assertEqual(metadata["department"], "토지정책과")
        self.assertIn("2017. 5. 4", metadata["distributed_at"])
        self.assertIn("044", metadata["contact_phone"])
        self.assertEqual(metadata["contacts"][0]["name"], "김상석")

    def test_explicit_heading_rules(self) -> None:
        blocks: list[hwp2md.Block] = []

        hwp2md.append_text_block(blocks, "[1] 지역별 지가변동률", "font")
        hwp2md.append_text_block(blocks, "가. 주요 현황", "font")

        self.assertEqual([(block.type, block.level, block.text) for block in blocks], [
            ("heading", 2, "1. 지역별 지가변동률"),
            ("heading", 3, "가. 주요 현황"),
        ])

    def test_inline_control_text_loss_uses_visible_text(self) -> None:
        visible = "* 전년대비 증가율(전체, %) : (‘12)2.6→(’13)0.5"
        decoded = visible.replace("2.6→", "\x012.6→")
        payload = decoded.encode("utf-16le")

        text = hwp2md.clean_hwp_text_payload(payload)

        self.assertIn("2.6→", text)

    def test_asset_markdown_uses_safe_destination(self) -> None:
        asset = hwp2md.Asset(source="BinData/BIN0001.bmp", output="image-001.png", kind="image", bytes=1)

        markdown = hwp2md.render_asset_markdown(asset, "원본 파일(테스트).assets", alt="그림")

        self.assertEqual(markdown, "![그림](<원본 파일(테스트).assets/image-001.png>)")

    def test_bindata_index_parses_hex_suffixes(self) -> None:
        self.assertEqual(hwp2md.parse_bindata_index("BIN0001.bmp"), 1)
        self.assertEqual(hwp2md.parse_bindata_index("BIN000A.jpg"), 10)
        self.assertIsNone(hwp2md.parse_bindata_index("image.png"))

    def test_known_layout_and_field_controls_are_classified(self) -> None:
        warnings: list[hwp2md.WarningItem] = []
        losses: list[hwp2md.LossItem] = []

        hwp2md.add_hwp_control_warnings({b"dces": 1, b"dloc": 2, b"pngp": 1, b"umf%": 3}, warnings, losses)

        codes = [item.code for item in warnings]
        self.assertIn("HWP_LAYOUT_CONTROLS_IGNORED", codes)
        self.assertIn("HWP_FIELD_CONTROLS_IGNORED", codes)
        self.assertNotIn("UNHANDLED_HWP_CONTROLS", codes)
        self.assertEqual(losses, [])

    def test_unknown_controls_still_warn(self) -> None:
        warnings: list[hwp2md.WarningItem] = []

        hwp2md.add_hwp_control_warnings({b"zzzz": 1}, warnings, [])

        self.assertEqual([item.code for item in warnings], ["UNHANDLED_HWP_CONTROLS"])

    def test_bmp_to_png_converts_simple_24bit_bmp(self) -> None:
        # 1x1 24-bit BMP, one red pixel. BMP stores pixels as BGR and rows are 4-byte padded.
        bmp = (
            b"BM"
            + (58).to_bytes(4, "little")
            + b"\x00\x00\x00\x00"
            + (54).to_bytes(4, "little")
            + (40).to_bytes(4, "little")
            + (1).to_bytes(4, "little", signed=True)
            + (1).to_bytes(4, "little", signed=True)
            + (1).to_bytes(2, "little")
            + (24).to_bytes(2, "little")
            + (0).to_bytes(4, "little")
            + (4).to_bytes(4, "little")
            + (0).to_bytes(4, "little", signed=True)
            + (0).to_bytes(4, "little", signed=True)
            + (0).to_bytes(4, "little")
            + (0).to_bytes(4, "little")
            + b"\x00\x00\xff\x00"
        )

        png = hwp2md.bmp_to_png(bmp)

        self.assertIsNotNone(png)
        self.assertTrue(png and png.startswith(b"\x89PNG"))
        self.assertEqual(hwp2md.image_dimensions_from_bytes(png or b""), (1, 1))

    def test_hwp_equation_fallback_extracts_fraction(self) -> None:
        payload = ("\x00\x00\x0c{8} over {100}\r\n\r\nϨ A Equation Version 60\x00HYhwpEQ").encode("utf-16le")

        equation = hwp2md.normalize_hwp_equation(payload)

        self.assertEqual(equation, r"\frac{8}{100}")

    def test_caption_following_image_anchor_prevents_duplicate_caption_insert(self) -> None:
        blocks = [
            hwp2md.Block(type="paragraph", text="< 그림 추이 >"),
            hwp2md.Block(type="image_anchor", text="< 그림 추이 >"),
        ]

        self.assertTrue(hwp2md.caption_is_for_following_image_anchor(blocks, 0))

    def test_render_table_generates_readable_fallback_headers(self) -> None:
        table = hwp2md.render_table([["1", "2"], ["3", "4"]], "gfm")

        self.assertEqual(table[0], "| 열1 | 열2 |")

    def test_clean_generated_assets_removes_only_current_stem_outputs(self) -> None:
        assets_dir = ROOT / "sample_output" / "_unit_assets"
        assets_dir.mkdir(exist_ok=True)
        generated = assets_dir / "문서-001.bmp"
        other = assets_dir / "다른문서-001.bmp"
        generated.write_bytes(b"old")
        other.write_bytes(b"keep")

        removed = hwp2md.clean_generated_assets(assets_dir, "문서")

        self.assertEqual(removed, 1)
        self.assertFalse(generated.exists())
        self.assertTrue(other.exists())
        other.unlink()
        assets_dir.rmdir()

    def test_render_markdown_places_image_anchor_once(self) -> None:
        assets_dir = ROOT / "sample_output" / "_unit_anchor.assets"
        assets_dir.mkdir(exist_ok=True)
        image = assets_dir / "image-001.png"
        image.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (300).to_bytes(4, "big")
            + (300).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        blocks = [
            hwp2md.Block(type="paragraph", text="< 그림 추이 >"),
            hwp2md.Block(type="image_anchor", text="< 그림 추이 >", alt="< 그림 추이 >"),
        ]
        assets = [hwp2md.Asset(source="BinData/BIN0001.png", output="image-001.png", kind="image", bytes=100)]
        report = {"format": "hwp", "metadata": {}}

        markdown = hwp2md.render_markdown(Path("sample.hwp"), blocks, assets, report, hwp2md.ConvertOptions(frontmatter="none"), ROOT / "sample_output" / "_unit_anchor.md", assets_dir)

        self.assertEqual(markdown.count("![< 그림 추이 >]"), 1)
        image.unlink()
        assets_dir.rmdir()

    def test_asset_position_summary_counts_caption_fallbacks(self) -> None:
        assets_dir = ROOT / "sample_output" / "_unit_position.assets"
        assets_dir.mkdir(exist_ok=True)
        image_a = assets_dir / "image-001.png"
        image_b = assets_dir / "image-002.png"
        png_header = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (300).to_bytes(4, "big")
            + (300).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        image_a.write_bytes(png_header)
        image_b.write_bytes(png_header)
        blocks = [hwp2md.Block(type="paragraph", text="< \ucd94\uc774 >")]
        assets = [
            hwp2md.Asset(source="BinData/BIN0001.png", output="image-001.png", kind="image", bytes=100),
            hwp2md.Asset(source="BinData/BIN0002.png", output="image-002.png", kind="image", bytes=100),
        ]

        summary = hwp2md.summarize_asset_placements(assets, blocks, assets_dir)

        self.assertEqual(summary.caption, 1)
        self.assertEqual(summary.approximated, 1)
        warnings: list[hwp2md.WarningItem] = []
        losses: list[hwp2md.LossItem] = []
        hwp2md.add_asset_position_warnings(assets, blocks, assets_dir, warnings, losses)
        self.assertIn("ASSET_POSITION_FROM_CAPTION", [item.code for item in warnings])
        self.assertIn(("ASSET_INLINE_POSITION_APPROXIMATED", 1), [(item.code, item.count) for item in losses])
        image_a.unlink()
        image_b.unlink()
        assets_dir.rmdir()


@unittest.skipUnless(SAMPLE_170508.exists(), "sample HWP file is not available")
class Hwp2MdSampleTests(unittest.TestCase):
    def test_170508_sample_restores_values_and_metadata(self) -> None:
        try:
            import olefile  # noqa: F401
        except Exception:
            self.skipTest("olefile is not installed")

        result = hwp2md.convert_hwp(
            SAMPLE_170508,
            ROOT / "sample_output" / "_unittest_170508.md",
            ROOT / "sample_output" / "_unittest_170508.assets",
            hwp2md.ConvertOptions(image_mode="skip"),
        )

        self.assertIn("metadata:", result.markdown)
        self.assertIn("distributed_at", result.markdown)
        self.assertIn("2.6→(’13)0.5", result.markdown)
        self.assertIn("14.7→(’13)38.2", result.markdown)
        self.assertNotIn("→)→", result.markdown)
        self.assertEqual(result.report["metadata"]["document_type"], "보도자료")


if __name__ == "__main__":
    unittest.main()
