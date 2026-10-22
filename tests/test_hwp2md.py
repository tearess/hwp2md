import struct
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

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

    def test_hwp_equation_record_reads_length_prefixed_script(self) -> None:
        # 실제 EQEDIT 레코드: 속성, 스크립트 글자 수, 스크립트, 그 뒤에 크기·버전·글꼴 이름이 붙는다.
        script = "{8} over {100}"
        tail = "Ϩ\x00\x00\x00A\x00\x13Equation Version 60\x09HancomEQN"
        payload = struct.pack("<IH", 0, len(script)) + script.encode("utf-16le") + tail.encode("utf-16le")

        self.assertEqual(hwp2md.normalize_hwp_equation(payload), r"\frac{8}{100}")

    def test_hwp_equation_falls_back_when_length_is_broken(self) -> None:
        payload = struct.pack("<IH", 0, 0xFFFF) + "7over100\r\n\r\nϨ\x00\x00\x00C\x00\x00\x07HYhwpEQ".encode("utf-16le")

        self.assertEqual(hwp2md.normalize_hwp_equation(payload), r"\frac{7}{100}")

    def test_hwp_equation_empty_script_is_none(self) -> None:
        payload = struct.pack("<IH", 0, 4) + "  \r\n".encode("utf-16le")

        self.assertIsNone(hwp2md.normalize_hwp_equation(payload))

    def test_hwp_equation_to_latex_structures(self) -> None:
        cases = {
            "{300,000,000} over {500,000,000}": r"\frac{300{,}000{,}000}{500{,}000{,}000}",
            "{110,000,000} over {200,000,000}  TIMES 20%": r"\frac{110{,}000{,}000}{200{,}000{,}000} \times 20 \%",
            "H _{n} = {a(r ^{n} -1)} over {r-1} (r != 1)": r"H_{n} = \frac{a ( r^{n} - 1 )}{r - 1} ( r \neq 1 )",
            "x = {-b +- sqrt{b^2 -4ac}} over {2a}": r"x = \frac{- b \pm \sqrt{b^{2} - 4 ac}}{2 a}",
            "sum from {i=1} to n i": r"\sum_{i = 1}^{n} i",
            "lim _{x -> 0} {sin x} over x": r"\lim_{x \rightarrow 0} \frac{\sin x}{x}",
            "root 3 of {x+1}": r"\sqrt[3]{x + 1}",
            " LEFT ( { 기정} atop {준공 }  RIGHT )   \r\n": r"\left( {\text{기정} \atop \text{준공}} \right)",
            "cases{x & x>=0 # -x & x<0}": r"\begin{cases} x & x \geq 0 \\ - x & x < 0 \end{cases}",
            "dmatrix{a&b#c&d}": r"\begin{vmatrix} a & b \\ c & d \end{vmatrix}",
            "alpha + BETA + GAMMA": r"\alpha + B + \Gamma",
            "a RARROW b rarrow c": r"a \Rightarrow b \rightarrow c",
            "{`토지``지분} over {감정평가액}": r"\frac{\, \text{토지} \, \, \text{지분}}{\text{감정평가액}}",
            "a # b": r"\begin{gathered} a \\ b \end{gathered}",
        }
        for script, expected in cases.items():
            with self.subTest(script=script):
                self.assertEqual(hwp2md.hwp_equation_to_latex(script), expected)

    def test_hwp_equation_unbalanced_input_stays_valid(self) -> None:
        # 짝이 맞지 않는 괄호나 LEFT/RIGHT가 있어도 LaTeX 괄호 짝은 맞아야 한다.
        self.assertEqual(hwp2md.hwp_equation_to_latex("{{a"), "{{a}}")
        self.assertEqual(hwp2md.hwp_equation_to_latex("}}a"), "a")
        self.assertEqual(hwp2md.hwp_equation_to_latex("LEFT ( x"), "( x")

    def test_escape_md_text_keeps_inline_math(self) -> None:
        text = hwp2md.escape_md_text(r"값은 \(\frac{a_1}{b}\) 이고 a_b")

        self.assertEqual(text, r"값은 \(\frac{a_1}{b}\) 이고 a\_b")

    def test_hwpx_equation_script_becomes_inline_latex(self) -> None:
        xml = (
            '<hp:p xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:run><hp:t>등락률 : </hp:t>'
            '<hp:equation><hp:shapeComment>수식입니다.</hp:shapeComment><hp:script>{a-b} over b</hp:script></hp:equation>'
            "</hp:run></hp:p>"
        )

        self.assertEqual(hwp2md.collect_text(ET.fromstring(xml)), r"등락률 : \(\frac{a - b}{b}\)")

    def test_aes128_decrypt_matches_fips197_vector(self) -> None:
        key = bytes(range(16))
        ciphertext = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

        self.assertEqual(hwp2md.aes128_ecb_decrypt(key, ciphertext * 2), bytes.fromhex("00112233445566778899aabbccddeeff") * 2)

    def test_distribution_view_text_is_decrypted(self) -> None:
        key = bytes(range(16))
        seed = 0x12345677
        plain = bytearray(256)
        plain[0:4] = struct.pack("<I", seed)
        offset = 4 + (seed & 0x0F)
        plain[offset : offset + 16] = key
        # 배포용 문서는 MSVC rand() 값으로 씨앗 뒤 252바이트를 XOR해 키를 숨긴다.
        state, count, value = seed, 0, 0
        for i in range(256):
            if count == 0:
                state = (state * 214013 + 2531011) & 0xFFFFFFFF
                value = (state >> 16) & 0xFF
                state = (state * 214013 + 2531011) & 0xFFFFFFFF
                count = ((state >> 16) & 0x0F) + 1
            if i >= 4:
                plain[i] ^= value
            count -= 1
        header = struct.pack("<I", hwp2md.HWP_DISTRIBUTE_DOC_DATA_TAG | (256 << 20))
        stream = header + bytes(plain) + bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a") + b"\x00" * 5

        self.assertEqual(hwp2md.decrypt_hwp_view_text(stream), bytes.fromhex("00112233445566778899aabbccddeeff"))

    def test_distribution_view_text_without_key_record_is_rejected(self) -> None:
        with self.assertRaises(hwp2md.ConversionError):
            hwp2md.decrypt_hwp_view_text(b"\x00" * 300)

    def test_caption_following_image_anchor_prevents_duplicate_caption_insert(self) -> None:
        blocks = [
            hwp2md.Block(type="paragraph", text="< 그림 추이 >"),
            hwp2md.Block(type="image_anchor", text="< 그림 추이 >"),
        ]

        self.assertTrue(hwp2md.caption_is_for_following_image_anchor(blocks, 0))

    def test_render_table_generates_readable_fallback_headers(self) -> None:
        table = hwp2md.render_table([["1", "2"], ["3", "4"]], "gfm")

        self.assertEqual(table[0], "| 열1 | 열2 |")

    def test_extended_control_is_skipped_as_eight_units(self) -> None:
        # 확장 제어문자는 제어코드 + 데이터 6유닛 + 제어코드(총 8유닛)이며, 뒤따르는 "1." 번호를 먹으면 안 된다.
        raw = "\x03汫╣\x00\x00\x00\x00\x031. 귀 대표회의의 무궁한 발전을 기원합니다."
        self.assertEqual(hwp2md.clean_hwp_decoded_text(raw, skip_extended_controls=True), "1. 귀 대표회의의 무궁한 발전을 기원합니다.")

    def test_ordered_list_keeps_original_numbers(self) -> None:
        blocks: list[hwp2md.Block] = []
        for text in ["2. 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신하여 드립니다.", "3. 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신하여 드립니다."]:
            hwp2md.append_text_block(blocks, text, "font", is_first=False)
        self.assertEqual([b.number for b in blocks], [2, 3])
        options = hwp2md.ConvertOptions(frontmatter="none")
        report = hwp2md.build_report(Path("a.hwp"), "hwp", blocks, [], [], [])
        markdown = hwp2md.render_markdown(Path("a.hwp"), blocks, [], report, options, Path("a.md"), Path("a.assets"))
        self.assertIn("2. 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신하여 드립니다.\n3. 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신하여 드립니다.", markdown)

    def test_korean_letter_markers_are_preserved(self) -> None:
        blocks: list[hwp2md.Block] = []
        hwp2md.append_text_block(blocks, "가. 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신하여 드립니다.", "font", is_first=False)
        self.assertEqual((blocks[0].type, blocks[0].text), ("bullet_list", "가. 위 호와 관련하여 입주자대표회의에서 요청한 사항에 대하여 아래와 같이 회신하여 드립니다."))

    def test_decimal_number_is_not_a_list_marker(self) -> None:
        blocks: list[hwp2md.Block] = []
        hwp2md.append_text_block(blocks, "0.0%미만", "none", is_first=False)
        self.assertEqual((blocks[0].type, blocks[0].text), ("paragraph", "0.0%미만"))

    def test_dropped_page_artifact_table_is_reported(self) -> None:
        blocks = [hwp2md.Block(type="page_artifact", text="협조자 시행 서울시 강남구")]
        warnings: list[hwp2md.WarningItem] = []
        losses: list[hwp2md.LossItem] = []
        hwp2md.add_page_artifact_warnings(blocks, warnings, losses)
        self.assertEqual(warnings[0].code, "PAGE_ARTIFACT_TABLE_DROPPED")
        self.assertIn("협조자", warnings[0].message)
        self.assertEqual(hwp2md.content_blocks(blocks), [])

    def test_clean_generated_assets_removes_only_current_stem_outputs(self) -> None:
        assets_dir = ROOT / "sample_output" / "_unit_assets"
        assets_dir.mkdir(exist_ok=True)
        generated = assets_dir / "image-001.bmp"
        unrelated = assets_dir / "image-002.png"
        hwp2md.write_asset_manifest(assets_dir, [hwp2md.Asset(source="s", output="image-001.bmp", kind="image", bytes=1)])
        legacy_generated = assets_dir / "문서-001.bmp"
        other = assets_dir / "다른문서-001.bmp"
        generated.write_bytes(b"old")
        unrelated.write_bytes(b"keep")
        legacy_generated.write_bytes(b"legacy")
        other.write_bytes(b"keep")

        removed = hwp2md.clean_generated_assets(assets_dir, "문서")

        self.assertEqual(removed, 2)
        self.assertFalse(generated.exists())
        self.assertFalse(legacy_generated.exists())
        self.assertTrue(other.exists())
        self.assertTrue(unrelated.exists(), "매니페스트에 없는 image-NNN 파일은 지우면 안 됩니다")
        other.unlink()
        unrelated.unlink()
        (assets_dir / hwp2md.ASSET_MANIFEST_NAME).unlink()
        assets_dir.rmdir()

    def test_generated_asset_name_stays_short_for_long_source_names(self) -> None:
        self.assertEqual(hwp2md.generated_asset_name(1, ".png"), "image-001.png")
        self.assertLess(len(hwp2md.generated_asset_name(999, "jpeg")), 20)

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
        self.assertIn("2.6 → (’13) 0.5", result.markdown)
        self.assertIn("14.7 → (’13) 38.2", result.markdown)
        self.assertNotIn("→)→", result.markdown)
        self.assertEqual(result.report["metadata"]["document_type"], "보도자료")


if __name__ == "__main__":
    unittest.main()
