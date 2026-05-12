#!/usr/bin/env python3
"""
hwp2md: HWP/HWPX to Markdown converter

이 도구는 HWP 5.x OLE 문서와 HWPX ZIP/XML 문서를 Markdown 초안으로 변환한다.
목표는 픽셀 단위 레이아웃 재현이 아니라, 사람이 읽고 편집할 수 있는 의미 중심 Markdown 생성이다.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import html
import json
import os
import re
import shutil
import struct
import sys
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

VERSION = "0.1.0"
HWP_CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
HWP_SIGNATURE = b"HWP Document File"
HWP_PARA_TEXT_TAG = 67
HWP_CTRL_HEADER_TAG = 71
HWP_TABLE_CELL_TAG = 72
HWP_TABLE_TAG = 77
HWP_TABLE_CTRL_ID = b" lbt"
HWP_SHAPE_CTRL_ID = b" osg"
HWP_EQUATION_RECORD_TAG = 88
HWP_EQUATION_CTRL_IDS = {b"deqe", b"eqed"}
HWP_FOOTNOTE_CTRL_IDS = {b"toof", b"foot", b" nf", b"fn  "}
HWP_LAYOUT_CTRL_IDS = {b"dces", b"dloc", b"pngp", b"dhgp", b"onwn", b"spct", b"tcgp", b"daeh"}
HWP_FIELD_CTRL_IDS = {b"umf%", b"klc%"}
HWP_PRIVATE_SQUARE_BULLET = "\U000f03da"
HWP_PRIVATE_NUMBER_BASE = 0xF02B0


@dataclasses.dataclass
class WarningItem:
    code: str
    message: str
    sourceRef: str | None = None


@dataclasses.dataclass
class LossItem:
    code: str
    count: int = 1


@dataclasses.dataclass
class Asset:
    source: str
    output: str
    kind: str = "binary"
    bytes: int = 0
    index: int | None = None
    width: int | None = None
    height: int | None = None


@dataclasses.dataclass
class TableCell:
    row: int
    col: int
    text: str = ""
    rowspan: int = 1
    colspan: int = 1


@dataclasses.dataclass
class Block:
    type: str
    text: str | None = None
    level: int | None = None
    rows: list[list[str]] | None = None
    cells: list[TableCell] | None = None
    path: str | None = None
    alt: str | None = None


@dataclasses.dataclass
class ConvertOptions:
    markdown_format: str = "gfm"
    heading_policy: str = "font"
    table_mode: str = "hybrid"
    image_mode: str = "extract"
    equation_mode: str = "latex"
    frontmatter: str = "yaml"
    include_page_artifacts: bool = False
    clean_assets: bool = True
    strict: bool = False
    dump_ir: bool = False


@dataclasses.dataclass
class ConvertResult:
    markdown: str
    report: dict[str, Any]
    blocks: list[Block]
    assets: list[Asset]


class ConversionError(Exception):
    """사용자에게 보여줄 수 있는 변환 오류."""


def detect_format(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(16)
    suffix = path.suffix.lower()
    if head.startswith(HWP_CFB_SIGNATURE):
        return "hwp"
    if zipfile.is_zipfile(path):
        return "hwpx"
    if suffix == ".hwp":
        return "hwp"
    if suffix == ".hwpx":
        return "hwpx"
    raise ConversionError(f"지원하지 않는 파일 형식입니다: {path}")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def safe_stem(path: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", path.stem).strip("._-")
    return stem or "document"


def source_stem(path: Path) -> str:
    stem = path.stem.strip()
    return stem or safe_stem(path)


def escape_md_text(text: str) -> str:
    # 문장 내부의 과도한 이스케이프는 가독성을 해치므로 Markdown 구조 문자 일부만 보수적으로 처리한다.
    text = text.replace("\\", "\\\\")
    text = re.sub(r"([*_`])", r"\\\1", text)
    return text


def clean_plain_text(text: str) -> str:
    text = text.replace("\ufeff", "")
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    # 3개 이상의 연속 빈 줄을 1개로 줄인다.
    compact: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            compact.append(line)
        else:
            blank += 1
            if blank <= 1:
                compact.append("")
    return "\n".join(compact).strip()


def clean_hwp_text_payload(payload: bytes) -> str:
    """HWP PARA_TEXT 레코드에서 사람이 읽을 수 있는 문자열을 최대한 보수적으로 추출한다.

    HWP 바이너리 텍스트에는 문단/표/그림/필드 제어 문자가 UTF-16 코드 단위 사이에
    삽입된다. 일부 inline 제어문자는 바로 뒤의 숫자/기호와 붙어 나타나므로, 구조적
    제거 결과가 본문을 잃었다고 판단되는 경우에는 제어문자 자체만 걷어낸 결과를 쓴다.
    """
    s = payload.decode("utf-16le", errors="ignore")
    structured = clean_hwp_decoded_text(s, skip_extended_controls=True)
    visible = clean_hwp_decoded_text(s, skip_extended_controls=False)
    if should_use_visible_hwp_text(structured, visible):
        return visible
    return structured


def clean_hwp_decoded_text(s: str, skip_extended_controls: bool) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        code = ord(ch)
        if ch in "\r\n\t":
            out.append("\n" if ch in "\r\n" else "\t")
            i += 1
            continue
        if code < 32:
            # 확장 제어문자는 대체로 control char + 4 UTF-16 units + control char 형태로 나타난다.
            if not skip_extended_controls:
                i += 1
            elif i + 5 < len(s) and s[i + 5] == ch:
                i += 6
            elif i + 4 < len(s):
                i += 5
            else:
                i += 1
            continue
        # 제어 시퀀스 잔여물에서 자주 보이는 역순 ASCII 토큰을 제거한다.
        if ch in {"捤", "獥", "汤", "捯", "桤", "灧", "湰", "湯", "湷", "瑣", "氠", "瑢", "慤", "桥"}:
            i += 1
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    # HWP 내부 객체 anchor가 남긴 짧은 영문 토큰 제거.
    text = re.sub(r"\b(?:dces|lcol|hdpg|pnpg|onwn|tcpg| lbt|dahe)\b", "", text)
    return clean_plain_text(text)


def should_use_visible_hwp_text(structured: str, visible: str) -> bool:
    if not visible or visible == structured:
        return False
    # 연도별 추이처럼 inline 제어문자가 숫자 앞에 붙으면 기존 방식은 "(‘12)→)" 꼴만 남긴다.
    if re.search(r"→\s*\)", structured) and visible.count("→") >= structured.count("→"):
        return count_digits(visible) > count_digits(structured)
    if visible.count("→") > structured.count("→") and count_digits(visible) >= count_digits(structured):
        return True
    if count_digits(visible) < count_digits(structured) + 2:
        return False
    return text_without_digits(structured) == text_without_digits(visible)


def count_digits(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


def text_without_digits(text: str) -> str:
    return re.sub(r"\d|[.,+\-△%]", "", text)


def parse_hwp_records(data: bytes) -> Iterable[tuple[int, int, bytes]]:
    pos = 0
    size_data = len(data)
    while pos + 4 <= size_data:
        header = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag_id = header & 0x3FF
        level = (header >> 10) & 0x3FF
        rec_size = (header >> 20) & 0xFFF
        if rec_size == 0xFFF:
            if pos + 4 > size_data:
                break
            rec_size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        if rec_size < 0 or pos + rec_size > size_data:
            break
        yield tag_id, level, data[pos : pos + rec_size]
        pos += rec_size


def is_hwp_table_control(payload: bytes) -> bool:
    return payload.startswith(HWP_TABLE_CTRL_ID)


def parse_hwp_table_dimensions(payload: bytes) -> Optional[tuple[int, int]]:
    if len(payload) < 8:
        return None
    rows = struct.unpack_from("<H", payload, 4)[0]
    cols = struct.unpack_from("<H", payload, 6)[0]
    if not (1 <= rows <= 500 and 1 <= cols <= 100):
        return None
    return rows, cols


def parse_hwp_table_cell_header(payload: bytes, rows: int, cols: int) -> Optional[TableCell]:
    if len(payload) < 16:
        return None
    col = struct.unpack_from("<H", payload, 8)[0]
    row = struct.unpack_from("<H", payload, 10)[0]
    colspan = max(1, struct.unpack_from("<H", payload, 12)[0])
    rowspan = max(1, struct.unpack_from("<H", payload, 14)[0])
    if row >= rows or col >= cols:
        return None
    return TableCell(row=row, col=col, rowspan=rowspan, colspan=colspan)


def parse_hwp_table_records(
    records: list[tuple[int, int, bytes]],
    start_idx: int,
    heading_policy: str,
    document_has_blocks: bool,
    include_page_artifacts: bool,
) -> Optional[tuple[list[Block], int]]:
    """HWP table control 주변 레코드를 Markdown용 table/paragraph 블록으로 복원한다.

    HWP 5.x 본문 레코드는 표 셀의 실제 텍스트를 일반 문단 레코드로 저장하되,
    그 앞에 table/cell 레코드로 row, column, span 정보를 둔다. 기존 v0.1.0처럼
    PARA_TEXT만 읽으면 표의 2차원 구조가 사라지므로 이 구간만 상태 기반으로 묶는다.
    """
    tag, table_level, payload = records[start_idx]
    if tag != HWP_CTRL_HEADER_TAG or not is_hwp_table_control(payload):
        return None

    rows: Optional[int] = None
    cols: Optional[int] = None
    cells: list[TableCell] = []
    current_cell: Optional[TableCell] = None
    idx = start_idx + 1
    saw_table_record = False

    while idx < len(records):
        rec_tag, rec_level, rec_payload = records[idx]
        if idx > start_idx + 1 and rec_level <= table_level:
            break

        if rec_tag == HWP_TABLE_TAG:
            dims = parse_hwp_table_dimensions(rec_payload)
            if dims:
                rows, cols = dims
                saw_table_record = True
        elif rec_tag == HWP_TABLE_CELL_TAG and rows and cols:
            parsed_cell = parse_hwp_table_cell_header(rec_payload, rows, cols)
            if parsed_cell:
                current_cell = parsed_cell
                cells.append(current_cell)
        elif current_cell is not None and rec_tag == HWP_CTRL_HEADER_TAG and rec_level > table_level + 1:
            control_id = rec_payload[:4]
            addition: Optional[str] = None
            if control_id in HWP_EQUATION_CTRL_IDS:
                equation = extract_hwp_equation(records, idx) or r"\text{HWP equation}"
                addition = rf"\({equation}\)"
            elif control_id in HWP_FOOTNOTE_CTRL_IDS:
                addition = "[^hwp-footnote]"
            if addition:
                current_cell.text = "\n".join(part for part in [current_cell.text, addition] if part)
        elif current_cell is not None and rec_tag == HWP_PARA_TEXT_TAG and rec_level > table_level + 1:
            text = clean_hwp_text_payload(rec_payload)
            if text:
                current_cell.text = "\n".join(part for part in [current_cell.text, text] if part)

        idx += 1

    if not saw_table_record or rows is None or cols is None or not cells:
        return None

    grid = build_hwp_table_grid(rows, cols, cells)
    table_text = " ".join(cell.text for cell in cells if cell.text).strip()
    if is_press_release_header_table(table_text) and not include_page_artifacts:
        metadata = parse_press_release_metadata(cells)
        blocks = [Block(type="metadata", text=json.dumps(metadata, ensure_ascii=False))] if metadata else []
        return blocks, idx
    if is_page_artifact_table(rows, cols, cells, grid) and not include_page_artifacts:
        return [], idx
    if should_render_hwp_table(rows, cols, cells, grid):
        return [Block(type="table", rows=grid, cells=cells)], idx

    flattened: list[Block] = []
    for cell in sorted(cells, key=lambda item: (item.row, item.col)):
        for part in split_text_to_candidate_blocks(cell.text):
            is_first = not document_has_blocks and not flattened
            append_text_block(flattened, part, heading_policy, is_first=is_first, include_page_artifacts=include_page_artifacts)
    return flattened, idx


def build_hwp_table_grid(rows: int, cols: int, cells: list[TableCell]) -> list[list[str]]:
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in cells:
        if 0 <= cell.row < rows and 0 <= cell.col < cols:
            grid[cell.row][cell.col] = format_table_cell_text(cell.text)
    return grid


def format_table_cell_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        private_bullet = strip_private_bullet(line)
        if private_bullet:
            line = f"- {private_bullet}"
        else:
            match = re.match(r"^(?:(?:[•·∙◦ㆍ□○〇■▶▷◇◆※ㅇ])\s*|(?:[-*+])\s+)(?P<body>.+)$", line)
            if match:
                line = f"- {match.group('body').strip()}"
        lines.append(line)
    return lines


def format_table_cell_text(text: str) -> str:
    return "<br>".join(format_table_cell_lines(text)).strip()


def should_render_hwp_table(rows: int, cols: int, cells: list[TableCell], grid: list[list[str]]) -> bool:
    if rows < 2 or cols < 2:
        return False
    non_empty_cells = sum(1 for cell in cells if cell.text.strip())
    if non_empty_cells < 4:
        return False
    multi_col_rows = sum(1 for row in grid if sum(1 for text in row if text.strip()) >= 2)
    if multi_col_rows >= 2:
        return True
    return any(cell.rowspan > 1 or cell.colspan > 1 for cell in cells)


def is_page_artifact_table(rows: int, cols: int, cells: list[TableCell], grid: list[list[str]]) -> bool:
    text = " ".join(cell.text for cell in cells if cell.text).strip()
    if not text:
        return True
    if is_press_release_header_table(text):
        return True
    if cols < 12:
        return False
    footer_keywords = ["시행", "접수", "협조자", "전화", "전송", "비공개", "서울시", "www.", "우 "]
    keyword_hits = sum(1 for keyword in footer_keywords if keyword in text)
    filled = sum(1 for row in grid for cell in row if cell.strip())
    density = filled / max(1, rows * cols)
    return keyword_hits >= 3 and density < 0.4


def is_press_release_header_table(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if "보도자료" not in compact:
        return False
    required = ["배포일시", "보도일시"]
    if not all(keyword in compact for keyword in required):
        return False
    return "담당" in compact or "담당부서" in compact


def parse_press_release_metadata(cells: list[TableCell]) -> dict[str, Any]:
    rows: dict[int, list[TableCell]] = {}
    for cell in cells:
        rows.setdefault(cell.row, []).append(cell)

    metadata: dict[str, Any] = {"document_type": "보도자료"}
    for row_cells in rows.values():
        ordered = sorted(row_cells, key=lambda item: item.col)
        texts = [clean_plain_text(cell.text.replace("\n", " ")) for cell in ordered]
        texts = [text for text in texts if text]
        for idx, text in enumerate(texts):
            label = re.sub(r"\s+", "", text)
            value = next_metadata_value(texts, idx)
            if label == "배포일시" and value:
                metadata["distributed_at"] = value
            elif label == "보도일시" and value:
                metadata["release_at"] = value
            elif label in {"담당부서", "담당/부서"} and value:
                metadata["department"] = value
            elif label == "부서" and value and "department" not in metadata:
                metadata["department"] = value
            elif label == "담당자" and value:
                metadata["contact"] = value

    if "contact" in metadata:
        phones = re.findall(r"\(?0\d{1,3}\)?[-)\s]?\d{3,4}[-\s]?\d{4}|\b\d{3,4}\b", metadata["contact"])
        if phones:
            metadata["contact_phone"] = ", ".join(phones)
        contacts = parse_press_contacts(metadata["contact"], metadata.get("department"))
        if contacts:
            metadata["contacts"] = contacts
    return metadata


def next_metadata_value(texts: list[str], label_index: int) -> Optional[str]:
    label_words = {"보도자료", "배포일시", "보도일시", "담당", "부서", "담당부서", "담당/부서", "담당자"}
    for value in texts[label_index + 1 :]:
        compact = re.sub(r"\s+", "", value)
        if compact in label_words:
            continue
        return value
    return None


def parse_press_contacts(text: str, department: Optional[str] = None) -> list[dict[str, str]]:
    cleaned = re.sub(r"[∙•]", " ", text)
    parts = re.split(r"☎|전화|Tel\.?", cleaned, maxsplit=1, flags=re.I)
    people_text = parts[0]
    phones = normalize_phone_numbers(parts[1] if len(parts) > 1 else "")
    people: list[dict[str, str]] = []
    for idx, item in enumerate(re.split(r"[,/]", people_text)):
        item = item.strip()
        if not item:
            continue
        match = re.search(r"(?P<role>[가-힣A-Za-z]+)\s+(?P<name>[가-힣]{2,4})$", item)
        if not match:
            continue
        contact = {"role": match.group("role"), "name": match.group("name")}
        if department:
            contact["department"] = department
        if idx < len(phones):
            contact["phone"] = phones[idx]
        elif len(phones) == 1:
            contact["phone"] = phones[0]
        people.append(contact)
    return people


def normalize_phone_numbers(text: str) -> list[str]:
    if not text:
        return []
    text = text.strip()
    prefix_match = re.search(r"\(?0\d{1,3}\)?", text)
    prefix = prefix_match.group(0) if prefix_match else ""
    numbers = re.findall(r"(?:\(?0\d{1,3}\)?[-)\s]?)?\d{3,4}[-\s]?\d{4}|\b\d{3,4}\b", text)
    normalized: list[str] = []
    for number in numbers:
        number = number.strip()
        if prefix and not re.search(r"^(0|\()", number):
            number = f"{prefix}-{number}"
        normalized.append(re.sub(r"\s+", " ", number))
    return normalized


def read_hwp_stream(ole: Any, stream: list[str] | str, compressed: bool) -> bytes:
    data = ole.openstream(stream).read()
    if compressed:
        try:
            return zlib.decompress(data, -15)
        except zlib.error:
            return zlib.decompress(data)
    return data


def import_olefile() -> Any:
    try:
        import olefile  # type: ignore
    except Exception as exc:  # pragma: no cover - 환경 안내용
        raise ConversionError(
            "HWP(.hwp) 변환에는 olefile 패키지가 필요합니다. "
            "`python -m pip install -r requirements.txt` 실행 후 다시 시도하세요."
        ) from exc
    return olefile


def ole_stream_name(parts: list[str]) -> str:
    return "/".join(parts)


def control_id_label(control_id: bytes) -> str:
    try:
        label = control_id.decode("ascii")
    except UnicodeDecodeError:
        label = control_id.hex()
    return label.replace("\x00", "").strip() or control_id.hex()


def control_count_summary(control_counts: dict[bytes, int]) -> str:
    return ", ".join(f"{control_id_label(key)}={count}" for key, count in sorted(control_counts.items(), key=lambda item: control_id_label(item[0])))


def add_hwp_control_warnings(
    control_counts: dict[bytes, int],
    warnings: list[WarningItem],
    losses: list[LossItem],
    equation_fallback_count: int = 0,
    footnote_fallback_count: int = 0,
) -> None:
    non_table = {key: value for key, value in control_counts.items() if key != HWP_TABLE_CTRL_ID}
    if not non_table:
        return
    equation_count = sum(count for key, count in non_table.items() if key in HWP_EQUATION_CTRL_IDS)
    footnote_count = sum(count for key, count in non_table.items() if key in HWP_FOOTNOTE_CTRL_IDS)
    equation_dropped = max(0, equation_count - equation_fallback_count)
    footnote_dropped = max(0, footnote_count - footnote_fallback_count)
    if equation_fallback_count:
        warnings.append(WarningItem("EQUATION_FALLBACK_USED", f"HWP 수식 control {equation_fallback_count}개를 Markdown 수식 fallback으로 보존했습니다."))
    if equation_dropped:
        warnings.append(WarningItem("EQUATION_CONTROL_NOT_PARSED", f"HWP 수식 control {equation_dropped}개는 아직 LaTeX/이미지로 복원하지 못했습니다."))
        losses.append(LossItem("EQUATION_DROPPED", equation_dropped))
    if footnote_fallback_count:
        warnings.append(WarningItem("FOOTNOTE_FALLBACK_USED", f"HWP 각주 control {footnote_fallback_count}개를 Markdown 각주 placeholder로 보존했습니다."))
    if footnote_dropped:
        warnings.append(WarningItem("FOOTNOTE_CONTROL_NOT_PARSED", f"HWP 각주 control {footnote_dropped}개는 아직 본문 각주로 복원하지 못했습니다."))
        losses.append(LossItem("FOOTNOTE_DROPPED", footnote_dropped))

    layout_controls = {key: value for key, value in non_table.items() if key in HWP_LAYOUT_CTRL_IDS}
    field_controls = {key: value for key, value in non_table.items() if key in HWP_FIELD_CTRL_IDS}
    if layout_controls:
        warnings.append(WarningItem("HWP_LAYOUT_CONTROLS_IGNORED", f"Markdown 의미 구조와 직접 대응하지 않는 HWP 레이아웃 control을 별도 손실 없이 건너뛰었습니다: {control_count_summary(layout_controls)}"))
    if field_controls:
        warnings.append(WarningItem("HWP_FIELD_CONTROLS_IGNORED", f"HWP 필드 marker control은 본문에 보이는 텍스트를 우선 사용하고 marker 자체는 건너뛰었습니다: {control_count_summary(field_controls)}"))

    handled = {HWP_SHAPE_CTRL_ID, *HWP_EQUATION_CTRL_IDS, *HWP_FOOTNOTE_CTRL_IDS, *HWP_LAYOUT_CTRL_IDS, *HWP_FIELD_CTRL_IDS}
    unhandled = {key: value for key, value in non_table.items() if key not in handled}
    if unhandled:
        summary = control_count_summary(unhandled)
        warnings.append(WarningItem("UNHANDLED_HWP_CONTROLS", f"아직 구조화하지 않은 HWP control: {summary}"))


def clean_generated_assets(assets_dir: Path, stem: str) -> int:
    if not assets_dir.exists():
        return 0
    removed = 0
    for path in assets_dir.iterdir():
        if not path.is_file():
            continue
        if re.fullmatch(re.escape(stem) + r"-\d{3}\.[0-9A-Za-z]+", path.name):
            path.unlink()
            removed += 1
    return removed


def extract_hwp_assets(ole: Any, input_path: Path, assets_dir: Path, image_mode: str, warnings: list[WarningItem], clean_assets: bool = True) -> list[Asset]:
    assets: list[Asset] = []
    if image_mode == "skip":
        return assets
    bin_streams = [s for s in ole.listdir(streams=True, storages=False) if s and s[0] == "BinData"]
    if not bin_streams:
        return assets
    assets_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(input_path)
    if clean_assets:
        removed = clean_generated_assets(assets_dir, stem)
        if removed:
            warnings.append(WarningItem("ASSETS_CLEANED", f"이전 변환에서 생성된 자산 {removed}개를 정리했습니다."))
    for idx, stream in enumerate(bin_streams, start=1):
        source_name = stream[-1]
        raw_data = ole.openstream(stream).read()
        data = decode_hwp_bindata(raw_data)
        ext = Path(source_name).suffix.lower()
        if not ext:
            ext = guess_extension(data)
        guessed_ext = guess_extension(data)
        if guessed_ext != ".bin" and guessed_ext != ext:
            ext = guessed_ext
        if ext == ".bmp":
            png_data = bmp_to_png(data)
            if png_data:
                data = png_data
                ext = ".png"
        out_name = f"{stem}-{idx:03d}{ext}"
        out_path = assets_dir / out_name
        out_path.write_bytes(data)
        width, height = image_dimensions_from_bytes(data)
        assets.append(Asset(source=ole_stream_name(stream), output=out_name, kind="image", bytes=len(data), index=parse_bindata_index(source_name), width=width, height=height))
    return assets


def parse_bindata_index(name: str) -> Optional[int]:
    match = re.search(r"BIN([0-9A-Fa-f]+)", name)
    if not match:
        return None
    try:
        return int(match.group(1), 16)
    except ValueError:
        return None


def decode_hwp_bindata(data: bytes) -> bytes:
    if guess_extension(data) != ".bin":
        return data
    for wbits in (-15, 15):
        try:
            decoded = zlib.decompress(data, wbits)
        except zlib.error:
            continue
        if guess_extension(decoded) != ".bin":
            return decoded
    return data


def guess_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def bmp_to_png(data: bytes) -> Optional[bytes]:
    """Convert simple uncompressed BMP data to PNG without external dependencies."""
    if not data.startswith(b"BM") or len(data) < 54:
        return None
    try:
        pixel_offset = struct.unpack_from("<I", data, 10)[0]
        dib_size = struct.unpack_from("<I", data, 14)[0]
        if dib_size < 40 or len(data) < 14 + dib_size:
            return None
        width = struct.unpack_from("<i", data, 18)[0]
        height = struct.unpack_from("<i", data, 22)[0]
        planes = struct.unpack_from("<H", data, 26)[0]
        bpp = struct.unpack_from("<H", data, 28)[0]
        compression = struct.unpack_from("<I", data, 30)[0]
    except struct.error:
        return None
    if planes != 1 or compression != 0 or width <= 0 or height == 0 or bpp not in {24, 32}:
        return None

    abs_height = abs(height)
    row_stride = ((width * bpp + 31) // 32) * 4
    if pixel_offset + row_stride * abs_height > len(data):
        return None

    raw = bytearray()
    for out_row in range(abs_height):
        src_row = abs_height - 1 - out_row if height > 0 else out_row
        row_start = pixel_offset + src_row * row_stride
        raw.append(0)  # PNG filter type: None
        for col in range(width):
            px = row_start + col * (bpp // 8)
            b, g, r = data[px], data[px + 1], data[px + 2]
            raw.extend((r, g, b))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, abs_height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def append_image_anchor_if_caption(blocks: list[Block]) -> bool:
    visible = content_blocks(blocks)
    if not visible:
        return False
    last = visible[-1]
    if last.type == "image_anchor":
        return False
    if last.text and is_figure_caption_text(last.text):
        blocks.append(Block(type="image_anchor", text=last.text, alt=last.text))
        return True
    return False


def extract_hwp_equation(records: list[tuple[int, int, bytes]], start_idx: int) -> Optional[str]:
    _tag, control_level, _payload = records[start_idx]
    for idx in range(start_idx + 1, len(records)):
        tag, level, payload = records[idx]
        if level <= control_level:
            break
        if tag == HWP_EQUATION_RECORD_TAG:
            return normalize_hwp_equation(payload)
    return None


def normalize_hwp_equation(payload: bytes) -> Optional[str]:
    text = payload.decode("utf-16le", errors="ignore")
    text = text.split("HYhwpEQ", 1)[0]
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[^\w가-힣+\-(){}\[\]^.]+", "", text)
    text = re.sub(r"[^\w가-힣+\-*/=().,{}\[\]^_ %]+$", "", text)
    if not text:
        return None
    return hwp_equation_to_latex(text)


def hwp_equation_to_latex(text: str) -> str:
    text = text.strip()
    simple_fraction = re.search(r"\{?\s*([0-9.]+)\s*\}?\s*over\s*\{?\s*([0-9.]+)\s*\}?", text, flags=re.I)
    if simple_fraction:
        return f"\\frac{{{simple_fraction.group(1)}}}{{{simple_fraction.group(2)}}}"
    fraction = re.fullmatch(r"(.+?)\s*over\s*(.+)", text, flags=re.I)
    if fraction:
        return f"\\frac{{{fraction.group(1).strip()}}}{{{fraction.group(2).strip()}}}"
    text = re.sub(r"\bover\b", r"\\over ", text, flags=re.I)
    text = re.sub(r"\btimes\b", r"\\times ", text, flags=re.I)
    text = re.sub(r"\bdiv\b", r"\\div ", text, flags=re.I)
    return text


def append_footnote_placeholder(blocks: list[Block], index: int) -> None:
    blocks.append(Block(type="footnote", text=f"HWP 각주 내용은 원본 문서의 footnote control #{index}에 있습니다."))


def count_blocks(blocks: list[Block], block_type: str) -> int:
    return sum(1 for block in content_blocks(blocks) if block.type == block_type)


def count_equation_fallbacks(blocks: list[Block]) -> int:
    total = count_blocks(blocks, "equation")
    for block in content_blocks(blocks):
        if block.type == "table":
            if block.cells:
                total += sum(cell.text.count(r"\(") for cell in block.cells)
            elif block.rows:
                total += sum(cell.count(r"\(") for row in block.rows for cell in row)
    return total


def count_footnote_fallbacks(blocks: list[Block]) -> int:
    total = count_blocks(blocks, "footnote")
    for block in content_blocks(blocks):
        if block.type == "table":
            if block.cells:
                total += sum(cell.text.count("[^hwp-footnote]") for cell in block.cells)
            elif block.rows:
                total += sum(cell.count("[^hwp-footnote]") for row in block.rows for cell in row)
    return total


@dataclasses.dataclass
class AssetPlacementSummary:
    control: int = 0
    caption: int = 0
    approximated: int = 0


def next_unplaced_inline_asset(inline_assets: list[Asset], placed_assets: set[str], start_index: int) -> tuple[Optional[Asset], int]:
    index = start_index
    while index < len(inline_assets) and inline_assets[index].output in placed_assets:
        index += 1
    if index >= len(inline_assets):
        return None, index
    asset = inline_assets[index]
    placed_assets.add(asset.output)
    return asset, index + 1


def summarize_asset_placements(assets: list[Asset], blocks: list[Block], assets_dir: Path) -> AssetPlacementSummary:
    if not assets:
        return AssetPlacementSummary()
    visible_blocks = content_blocks(blocks)
    inline_assets = select_inline_assets(assets, assets_dir)
    inline_asset_index = 0
    placed_assets: set[str] = set()
    summary = AssetPlacementSummary()
    for idx, block in enumerate(visible_blocks):
        if block.type == "image_anchor":
            asset, inline_asset_index = next_unplaced_inline_asset(inline_assets, placed_assets, inline_asset_index)
            if asset:
                summary.control += 1
            continue
        if (
            block.text
            and is_figure_caption_text(block.text)
            and not caption_is_for_following_image_anchor(visible_blocks, idx)
            and not caption_is_for_following_table(visible_blocks, idx)
        ):
            asset, inline_asset_index = next_unplaced_inline_asset(inline_assets, placed_assets, inline_asset_index)
            if asset:
                summary.caption += 1
    summary.approximated = max(0, len(assets) - len(placed_assets))
    return summary


def add_asset_position_warnings(assets: list[Asset], blocks: list[Block], assets_dir: Path, warnings: list[WarningItem], losses: list[LossItem]) -> None:
    if not assets:
        return
    summary = summarize_asset_placements(assets, blocks, assets_dir)
    anchored = summary.control
    approximated = summary.approximated
    if anchored:
        warnings.append(WarningItem("ASSET_POSITION_FROM_CONTROL", f"HWP 그림 control 위치를 기준으로 이미지 {anchored}개를 본문에 배치했습니다."))
    if summary.caption:
        warnings.append(WarningItem("ASSET_POSITION_FROM_CAPTION", f"\ucea1\uc158\uc744 \uae30\uc900\uc73c\ub85c \uc774\ubbf8\uc9c0 {summary.caption}\uac1c\ub97c \ubcf8\ubb38\uc5d0 \ubc30\uce58\ud588\uc2b5\ub2c8\ub2e4."))
    if approximated:
        warnings.append(WarningItem("ASSET_POSITION_APPROXIMATED", f"HWP 이미지 {approximated}개는 정확한 본문 위치를 확정하지 못해 캡션 추정 또는 첨부 목록으로 보존했습니다."))
        losses.append(LossItem("ASSET_INLINE_POSITION_APPROXIMATED", approximated))


def convert_hwp(input_path: Path, output_path: Path, assets_dir: Path, options: ConvertOptions) -> ConvertResult:
    olefile = import_olefile()
    if not olefile.isOleFile(str(input_path)):
        raise ConversionError("OLE Compound File 형식의 HWP 문서가 아닙니다.")

    warnings: list[WarningItem] = []
    losses: list[LossItem] = []
    blocks: list[Block] = []
    assets: list[Asset] = []

    ole = olefile.OleFileIO(str(input_path))
    try:
        if not ole.exists("FileHeader"):
            raise ConversionError("HWP FileHeader 스트림을 찾을 수 없습니다.")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(HWP_SIGNATURE):
            warnings.append(WarningItem("HWP_SIGNATURE_UNEXPECTED", "FileHeader signature가 일반적인 HWP signature와 다릅니다."))
        flags = struct.unpack("<I", header[36:40])[0] if len(header) >= 40 else 0
        compressed = bool(flags & 0x01)
        encrypted = bool(flags & 0x02)
        if encrypted:
            raise ConversionError("암호화된 HWP 문서는 지원하지 않습니다.")

        sections = [s for s in ole.listdir(streams=True, storages=False) if len(s) == 2 and s[0] == "BodyText" and s[1].lower().startswith("section")]
        sections.sort(key=lambda s: int(re.sub(r"\D+", "", s[1]) or "0"))
        if not sections:
            raise ConversionError("BodyText/Section 스트림을 찾을 수 없습니다.")

        para_count = 0
        record_count = 0
        control_counts: dict[bytes, int] = {}
        for section in sections:
            try:
                data = read_hwp_stream(ole, section, compressed)
            except Exception as exc:
                warnings.append(WarningItem("SECTION_DECOMPRESS_FAILED", f"{ole_stream_name(section)} 압축 해제 실패: {exc}", ole_stream_name(section)))
                continue
            records = list(parse_hwp_records(data))
            for rec_tag, _rec_level, rec_payload in records:
                if rec_tag == HWP_CTRL_HEADER_TAG:
                    control_id = rec_payload[:4]
                    control_counts[control_id] = control_counts.get(control_id, 0) + 1
            idx = 0
            while idx < len(records):
                tag_id, level, payload = records[idx]
                record_count += 1
                if tag_id == HWP_CTRL_HEADER_TAG:
                    control_id = payload[:4]
                    if is_hwp_table_control(payload):
                        parsed_table = parse_hwp_table_records(
                        records,
                        idx,
                        options.heading_policy,
                        document_has_blocks=bool(content_blocks(blocks)),
                        include_page_artifacts=options.include_page_artifacts,
                    )
                        if parsed_table:
                            table_blocks, next_idx = parsed_table
                            blocks.extend(table_blocks)
                            record_count += max(0, next_idx - idx - 1)
                            idx = next_idx
                            continue
                    if control_id == HWP_SHAPE_CTRL_ID and options.image_mode != "skip":
                        append_image_anchor_if_caption(blocks)
                    elif control_id in HWP_EQUATION_CTRL_IDS and options.equation_mode != "skip":
                        equation = extract_hwp_equation(records, idx)
                        blocks.append(Block(type="equation", text=equation or r"\text{HWP equation}"))
                    elif control_id in HWP_FOOTNOTE_CTRL_IDS:
                        append_footnote_placeholder(blocks, count_blocks(blocks, "footnote") + 1)
                if tag_id == HWP_PARA_TEXT_TAG:
                    para_count += 1
                    text = clean_hwp_text_payload(payload)
                    for part in split_text_to_candidate_blocks(text):
                        append_text_block(blocks, part, options.heading_policy, is_first=not content_blocks(blocks), include_page_artifacts=options.include_page_artifacts)
                idx += 1

        if options.image_mode != "skip":
            assets = extract_hwp_assets(ole, input_path, assets_dir, options.image_mode, warnings, clean_assets=options.clean_assets)

        if record_count == 0:
            warnings.append(WarningItem("NO_HWP_RECORDS", "본문 레코드를 찾지 못했습니다."))
        if not blocks:
            warnings.append(WarningItem("NO_TEXT_BLOCKS", "추출된 텍스트 블록이 없습니다."))
            if ole.exists("PrvText"):
                try:
                    preview = ole.openstream("PrvText").read().decode("utf-16le", errors="ignore")
                    for part in split_text_to_candidate_blocks(clean_plain_text(preview)):
                        append_text_block(blocks, part, options.heading_policy, include_page_artifacts=options.include_page_artifacts)
                    warnings.append(WarningItem("PREVIEW_TEXT_USED", "BodyText 추출 결과가 비어 있어 PrvText를 fallback으로 사용했습니다."))
                except Exception:
                    pass

        losses.append(LossItem("LAYOUT_DROPPED", 1))
        losses.append(LossItem("CHARACTER_STYLE_PARTIALLY_DROPPED", 1))
        add_asset_position_warnings(assets, blocks, assets_dir, warnings, losses)
        add_hwp_control_warnings(
            control_counts,
            warnings,
            losses,
            equation_fallback_count=count_equation_fallbacks(blocks),
            footnote_fallback_count=count_footnote_fallbacks(blocks),
        )

        report = build_report(input_path, "hwp", blocks, assets, warnings, losses)
        markdown = render_markdown(input_path, blocks, assets, report, options, output_path, assets_dir)
        return ConvertResult(markdown=markdown, report=report, blocks=blocks, assets=assets)
    finally:
        ole.close()


def split_text_to_candidate_blocks(text: str) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # HWP 문단 안에 포함된 탭은 표 후보로 보존한다.
        lines.append(line)
    return lines


def normalize_hwp_leading_marker(text: str) -> str:
    # HWP 글꼴의 PUA 글머리표는 그대로 두면 Markdown에서 의미 없는 네모/기호로 보인다.
    return text.strip()


def parse_private_number_heading(text: str) -> Optional[tuple[int, str]]:
    stripped = text.strip()
    if not stripped:
        return None
    marker = ord(stripped[0])
    number = marker - HWP_PRIVATE_NUMBER_BASE
    if 1 <= number <= 20 and len(stripped) > 1:
        body = stripped[1:].strip()
        if body:
            return number, body
    return None


def strip_private_bullet(text: str) -> Optional[str]:
    stripped = text.strip()
    if stripped.startswith(HWP_PRIVATE_SQUARE_BULLET):
        body = stripped[1:].strip()
        return body or None
    return None


def is_page_artifact_text(text: str) -> bool:
    stripped = text.strip()
    if re.fullmatch(r"\d{1,3}", stripped):
        return True
    if stripped in {"╣", "║", "│", "┃"}:
        return True
    return False


def append_text_block(
    blocks: list[Block],
    text: str,
    heading_policy: str,
    is_first: Optional[bool] = None,
    include_page_artifacts: bool = False,
) -> None:
    if not text:
        return
    text = normalize_hwp_leading_marker(text)
    if is_page_artifact_text(text) and not include_page_artifacts:
        return
    private_heading = parse_private_number_heading(text)
    if private_heading:
        number, body = private_heading
        blocks.append(Block(type="heading", text=f"{number}. {body}", level=2))
        return
    private_bullet = strip_private_bullet(text)
    if private_bullet:
        blocks.append(Block(type="bullet_list", text=private_bullet))
        return
    table_rows = parse_table_like_text(text)
    if table_rows:
        blocks.append(Block(type="table", rows=table_rows))
        return
    explicit_heading = parse_explicit_heading(text)
    if explicit_heading:
        level, heading_text = explicit_heading
        blocks.append(Block(type="heading", text=heading_text, level=level))
        return
    list_match = re.match(r"^(?P<marker>(?:[-*+])|(?:[•·∙◦ㆍ□○〇■▶▷◇◆※ㅇ])|(?:\(?\d{1,3}\)?[.)])|(?:[가-하]\.)|(?:[A-Za-z][.)]))\s*(?P<body>.+)$", text)
    if list_match:
        marker = list_match.group("marker")
        body = list_match.group("body").strip()
        kind = "ordered_list" if re.match(r"^\(?\d+\)?[.)]$", marker) else "bullet_list"
        blocks.append(Block(type=kind, text=body))
        return
    level = infer_heading_level(text, heading_policy, is_first=(not blocks if is_first is None else is_first))
    if level:
        blocks.append(Block(type="heading", text=text, level=level))
    else:
        blocks.append(Block(type="paragraph", text=text))


def parse_explicit_heading(text: str) -> Optional[tuple[int, str]]:
    stripped = text.strip()
    if not stripped:
        return None
    attach_match = re.match(r"^【\s*붙임\s*】\s*(?P<body>.+)$", stripped)
    if attach_match:
        return 2, attach_match.group("body").strip()
    if re.fullmatch(r"붙임\s*\d*", stripped):
        return 2, stripped
    bracket_match = re.match(r"^\[(?P<num>\d{1,2})\]\s*(?P<body>.+)$", stripped)
    if bracket_match and is_short_heading_phrase(bracket_match.group("body")):
        return 2, f"{bracket_match.group('num')}. {bracket_match.group('body').strip()}"
    number_match = re.match(r"^(?P<num>\d{1,2})\.\s+(?P<body>.+)$", stripped)
    if number_match and is_short_heading_phrase(number_match.group("body")):
        return 2, stripped
    korean_match = re.match(r"^(?P<num>[가-하])\.\s+(?P<body>.+)$", stripped)
    if korean_match and is_short_heading_phrase(korean_match.group("body")):
        return 3, stripped
    return None


def is_short_heading_phrase(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 45:
        return False
    if re.search(r"[,;，；]", stripped):
        return False
    if stripped.endswith((".", ",", ";", ":")):
        return False
    if re.search(r"(다|요|음|임|함|됨|였다|한다|한다는|있다|없다)\.?$", stripped):
        return False
    return len(stripped.split()) <= 7


def infer_heading_level(text: str, policy: str, is_first: bool = False) -> Optional[int]:
    if policy == "none":
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if is_first and len(stripped) <= 80:
        return 1
    if re.match(r"^(제\s*\d+\s*[장절관항]|제\s*[一二三四五六七八九十]+\s*[장절관항])\b", stripped):
        return 2
    if re.match(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.)]\s+", stripped):
        return 2
    if re.match(r"^\d+\.\s+\S", stripped) and len(stripped) <= 80:
        return 2
    if re.match(r"^\d+(?:\.\d+)+\.?\s+\S", stripped) and len(stripped) <= 90:
        depth = stripped.split()[0].count(".")
        return min(6, max(3, depth + 1))
    if policy == "font" and len(stripped) <= 40:
        # 문서 제목 후보: 종결 어미 문장이나 긴 설명문은 제목으로 승격하지 않는다.
        if not re.search(r"[다요음임함됨됨]\.?$", stripped) and not stripped.endswith((".", ",", ";", ":")):
            if re.search(r"(보고|계획|검토|회의|현황|경위|근거|의견|총괄|요구사항|질의|회신|안내|지침|공문|제목)$", stripped):
                return 2
    return None


def parse_table_like_text(text: str) -> Optional[list[list[str]]]:
    if "\t" not in text:
        return None
    rows = []
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) >= 2:
            rows.append(cells)
    return rows if rows else None


def convert_hwpx(input_path: Path, output_path: Path, assets_dir: Path, options: ConvertOptions) -> ConvertResult:
    warnings: list[WarningItem] = []
    losses: list[LossItem] = []
    blocks: list[Block] = []
    assets: list[Asset] = []

    with zipfile.ZipFile(input_path) as zf:
        names = zf.namelist()
        section_names = sorted([n for n in names if re.search(r"(?:^|/)section\d+\.xml$", n, re.I)])
        if not section_names:
            # 일부 HWPX는 Contents/content.hpf에서 section 목록을 간접 참조한다.
            section_names = sorted([n for n in names if n.lower().endswith(".xml") and "section" in n.lower()])
        if not section_names:
            raise ConversionError("HWPX section XML을 찾을 수 없습니다.")

        for section_name in section_names:
            try:
                root = ET.fromstring(zf.read(section_name))
            except Exception as exc:
                warnings.append(WarningItem("HWPX_XML_PARSE_FAILED", f"{section_name} 파싱 실패: {exc}", section_name))
                continue
            walk_hwpx_blocks(root, blocks, options.heading_policy, options.include_page_artifacts)

        if options.image_mode != "skip":
            bin_names = [n for n in names if n.lower().startswith("bindata/") and not n.endswith("/")]
            if bin_names:
                assets_dir.mkdir(parents=True, exist_ok=True)
            stem = safe_stem(input_path)
            if bin_names and options.clean_assets:
                removed = clean_generated_assets(assets_dir, stem)
                if removed:
                    warnings.append(WarningItem("ASSETS_CLEANED", f"이전 변환에서 생성된 자산 {removed}개를 정리했습니다."))
            for idx, name in enumerate(bin_names, start=1):
                data = zf.read(name)
                ext = Path(name).suffix.lower() or guess_extension(data)
                if ext == ".bmp":
                    png_data = bmp_to_png(data)
                    if png_data:
                        data = png_data
                        ext = ".png"
                out_name = f"{stem}-{idx:03d}{ext}"
                (assets_dir / out_name).write_bytes(data)
                width, height = image_dimensions_from_bytes(data)
                assets.append(Asset(source=name, output=out_name, kind="image", bytes=len(data), index=parse_bindata_index(name), width=width, height=height))
            if assets:
                warnings.append(WarningItem("ASSET_POSITION_APPROXIMATED", "HWPX 이미지의 본문 내 배치 위치를 완전히 해석하지 못한 항목은 첨부 목록으로 보존합니다."))

    if not blocks:
        warnings.append(WarningItem("NO_TEXT_BLOCKS", "추출된 텍스트 블록이 없습니다."))
    losses.append(LossItem("LAYOUT_DROPPED", 1))
    losses.append(LossItem("CHARACTER_STYLE_PARTIALLY_DROPPED", 1))
    report = build_report(input_path, "hwpx", blocks, assets, warnings, losses)
    markdown = render_markdown(input_path, blocks, assets, report, options, output_path, assets_dir)
    return ConvertResult(markdown=markdown, report=report, blocks=blocks, assets=assets)


def local_name(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag


def collect_text(elem: ET.Element) -> str:
    return clean_plain_text(collect_text_with_inline_styles(elem))


def collect_text_with_inline_styles(elem: ET.Element) -> str:
    name = local_name(elem)
    if name in {"lineBreak", "br"}:
        return "\n"
    if name == "tab":
        return "\t"
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        parts.append(collect_text_with_inline_styles(child))
        if child.tail:
            parts.append(child.tail)
    text = "".join(parts)
    lower_name = name.lower()
    if lower_name in {"sup", "supscript", "superscript"}:
        return f"<sup>{html.escape(text)}</sup>"
    if lower_name in {"sub", "subscript"}:
        return f"<sub>{html.escape(text)}</sub>"
    if lower_name in {"u", "underline"}:
        return f"<u>{html.escape(text)}</u>"
    if lower_name in {"strong", "bold", "b"}:
        return f"**{text}**"
    if lower_name in {"em", "italic", "i"}:
        return f"*{text}*"
    return text


def walk_hwpx_blocks(elem: ET.Element, blocks: list[Block], heading_policy: str, include_page_artifacts: bool = False) -> None:
    name = local_name(elem)
    if name in {"tbl", "table"}:
        rows: list[list[str]] = []
        for tr in [x for x in elem.iter() if local_name(x) in {"tr", "row"}]:
            cells = []
            for tc in [x for x in list(tr) if local_name(x) in {"tc", "cell"}]:
                cells.append(collect_text(tc).replace("\n", " ").strip())
            if cells:
                rows.append(cells)
        if rows:
            blocks.append(Block(type="table", rows=rows))
        return
    if name == "p":
        # 표 내부 문단 중복은 상위 tbl 처리에서 걸러진다. 단독 문단만 처리한다.
        text = collect_text(elem)
        if text:
            for part in split_text_to_candidate_blocks(text):
                append_text_block(blocks, part, heading_policy, include_page_artifacts=include_page_artifacts)
        return
    for child in list(elem):
        walk_hwpx_blocks(child, blocks, heading_policy, include_page_artifacts)


def collect_metadata(blocks: list[Block]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for block in blocks:
        if block.type != "metadata" or not block.text:
            continue
        try:
            item = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            metadata.update(item)
    return metadata


def content_blocks(blocks: list[Block]) -> list[Block]:
    return [block for block in blocks if block.type != "metadata"]


def build_report(input_path: Path, fmt: str, blocks: list[Block], assets: list[Asset], warnings: list[WarningItem], losses: list[LossItem]) -> dict[str, Any]:
    loss_map: dict[str, int] = {}
    for item in losses:
        loss_map[item.code] = loss_map.get(item.code, 0) + item.count
    visible_blocks = content_blocks(blocks)
    metadata = collect_metadata(blocks)
    return {
        "source": str(input_path),
        "format": fmt,
        "converter": f"hwp2md {VERSION}",
        "blocks": len(visible_blocks),
        "blockTypes": {kind: sum(1 for b in visible_blocks if b.type == kind) for kind in sorted({b.type for b in visible_blocks})},
        "assets": len(assets),
        "assetItems": [dataclasses.asdict(asset) for asset in assets],
        "metadata": metadata,
        "warnings": [dataclasses.asdict(w) for w in warnings],
        "losses": [{"code": code, "count": count} for code, count in sorted(loss_map.items())],
    }


def render_markdown(input_path: Path, blocks: list[Block], assets: list[Asset], report: dict[str, Any], options: ConvertOptions, output_path: Path, assets_dir: Path) -> str:
    lines: list[str] = []
    visible_blocks = content_blocks(blocks)
    if options.frontmatter == "yaml":
        lines.extend(render_frontmatter(input_path, report, len(visible_blocks), len(assets)))

    rel_assets_dir = os.path.relpath(assets_dir, output_path.parent).replace(os.sep, "/") if assets else ""
    inline_assets = select_inline_assets(assets, assets_dir)
    inline_asset_index = 0
    placed_assets: set[str] = set()
    last_list_type: Optional[str] = None
    ordered_index = 1
    footnotes: list[str] = []
    for idx, block in enumerate(visible_blocks):
        if block.type == "heading":
            lines.append(f"{'#' * max(1, min(block.level or 2, 6))} {escape_md_text(block.text or '')}")
            lines.append("")
            last_list_type = None
        elif block.type == "paragraph":
            lines.append(escape_md_text(block.text or ""))
            lines.append("")
            last_list_type = None
        elif block.type == "bullet_list":
            lines.append(f"- {escape_md_text(block.text or '')}")
            last_list_type = block.type
        elif block.type == "ordered_list":
            if last_list_type != block.type:
                ordered_index = 1
            lines.append(f"{ordered_index}. {escape_md_text(block.text or '')}")
            ordered_index += 1
            last_list_type = block.type
        elif block.type == "table" and block.rows:
            lines.extend(render_table(block.rows, options.table_mode, block.cells))
            lines.append("")
            last_list_type = None
        elif block.type == "image" and block.path:
            lines.append(f"![{escape_md_text(block.alt or '')}]({block.path})")
            lines.append("")
            last_list_type = None
        elif block.type == "image_anchor":
            asset, inline_asset_index = next_unplaced_inline_asset(inline_assets, placed_assets, inline_asset_index)
            if asset:
                lines.append(render_asset_markdown(asset, rel_assets_dir, alt=block.alt or block.text))
                lines.append("")
            last_list_type = None
        elif block.type == "equation":
            equation = (block.text or r"\text{HWP equation}").strip()
            lines.extend(["$$", equation, "$$", ""])
            last_list_type = None
        elif block.type == "footnote":
            footnotes.append(block.text or "HWP 각주 내용은 원본 문서에 있습니다.")
            lines.append(f"[^{len(footnotes)}]")
            lines.append("")
            last_list_type = None

        if (
            block.type != "image_anchor"
            and
            block.text
            and inline_asset_index < len(inline_assets)
            and is_figure_caption_text(block.text)
            and not caption_is_for_following_image_anchor(visible_blocks, idx)
            and not caption_is_for_following_table(visible_blocks, idx)
        ):
            asset, inline_asset_index = next_unplaced_inline_asset(inline_assets, placed_assets, inline_asset_index)
            if asset:
                lines.append(render_asset_markdown(asset, rel_assets_dir, alt=block.text))
                lines.append("")

    if assets and options.image_mode != "skip":
        remaining_assets = [asset for asset in assets if asset.output not in placed_assets]
        if remaining_assets:
            lines.append("## 첨부 자산")
            lines.append("")
            for asset in remaining_assets:
                lines.append(render_asset_markdown(asset, rel_assets_dir))
                lines.append("")

    if footnotes:
        lines.append("## 각주")
        lines.append("")
        for idx, footnote in enumerate(footnotes, start=1):
            lines.append(f"[^{idx}]: {escape_md_text(footnote)}")
        lines.append("")

    md = "\n".join(lines).strip() + "\n"
    md = re.sub(r"\n{4,}", "\n\n\n", md)
    return md


def render_frontmatter(input_path: Path, report: dict[str, Any], block_count: int, asset_count: int) -> list[str]:
    lines = [
        "---",
        f"source: {json.dumps(str(input_path), ensure_ascii=False)}",
        f"format: {report['format']}",
        f"converted_by: hwp2md {VERSION}",
        f"blocks: {block_count}",
        f"assets: {asset_count}",
    ]
    metadata = report.get("metadata") or {}
    if metadata:
        lines.append("metadata:")
        for key in sorted(metadata):
            lines.append(f"  {key}: {json.dumps(metadata[key], ensure_ascii=False)}")
    lines.extend(["---", ""])
    return lines


def render_asset_markdown(asset: Asset, rel_assets_dir: str, alt: Optional[str] = None) -> str:
    rel = f"{rel_assets_dir}/{asset.output}" if rel_assets_dir and rel_assets_dir != "." else asset.output
    suffix = Path(asset.output).suffix.lower()
    label = escape_md_text(alt or Path(asset.output).stem)
    if asset.kind == "image" and suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
        return f"![{label}]({markdown_link_destination(rel)})"
    return f"- [{escape_md_text(asset.output)}]({markdown_link_destination(rel)})"


def markdown_link_destination(path: str) -> str:
    rel = path.replace("\\", "/")
    rel = rel.replace("<", "%3C").replace(">", "%3E")
    if re.search(r"[\s()]", rel):
        return f"<{rel}>"
    return rel


def select_inline_assets(assets: list[Asset], assets_dir: Path) -> list[Asset]:
    selected: list[Asset] = []
    for asset in assets:
        suffix = Path(asset.output).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
            continue
        width, height = image_dimensions(assets_dir / asset.output)
        if width and height:
            if max(width, height) < 250:
                continue
            if width * height < 80_000:
                continue
        elif asset.bytes < 30_000:
            continue
        selected.append(asset)
    return selected


def image_dimensions(path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    return image_dimensions_from_bytes(data)


def image_dimensions_from_bytes(data: bytes) -> tuple[Optional[int], Optional[int]]:
    if data.startswith(b"BM") and len(data) >= 26:
        return struct.unpack_from("<I", data, 18)[0], abs(struct.unpack_from("<i", data, 22)[0])
    if data.startswith(b"\x89PNG") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            pos += 2
            if marker in {0xD8, 0xD9}:
                continue
            if pos + 2 > len(data):
                break
            segment_size = int.from_bytes(data[pos : pos + 2], "big")
            if 0xC0 <= marker <= 0xC3 or 0xC5 <= marker <= 0xC7 or 0xC9 <= marker <= 0xCB or 0xCD <= marker <= 0xCF:
                if pos + 7 <= len(data):
                    height = int.from_bytes(data[pos + 3 : pos + 5], "big")
                    width = int.from_bytes(data[pos + 5 : pos + 7], "big")
                    return width, height
            pos += max(segment_size, 2)
    return None, None


def is_figure_caption_text(text: str) -> bool:
    stripped = text.strip()
    if not re.fullmatch(r"[<〈].+[>〉]", stripped):
        return False
    return bool(re.search(r"(추이|현황|분포|지도|색인도|그래프|도표|거래량|변동률|보유)", stripped))


def caption_is_for_following_table(blocks: list[Block], idx: int) -> bool:
    checked = 0
    for next_block in blocks[idx + 1 :]:
        if next_block.type == "image_anchor":
            return False
        if next_block.type == "table":
            return True
        if next_block.type in {"heading", "bullet_list", "ordered_list"}:
            return False
        if next_block.type == "paragraph":
            text = (next_block.text or "").strip()
            if not text:
                continue
            if re.match(r"^\(?단위\s*[:：]", text):
                checked += 1
                if checked <= 2:
                    continue
            return False
    return False


def caption_is_for_following_image_anchor(blocks: list[Block], idx: int) -> bool:
    for next_block in blocks[idx + 1 :]:
        if next_block.type == "image_anchor":
            return True
        if next_block.type in {"heading", "bullet_list", "ordered_list", "table", "image", "equation", "footnote"}:
            return False
        if next_block.type == "paragraph" and (next_block.text or "").strip():
            return False
    return False


def render_table(rows: list[list[str]], table_mode: str, cells: Optional[list[TableCell]] = None) -> list[str]:
    max_cols = max((len(r) for r in rows), default=0)
    if max_cols == 0:
        return []
    norm = [(r + [""] * max_cols)[:max_cols] for r in rows]
    has_spans = bool(cells and any(cell.rowspan > 1 or cell.colspan > 1 for cell in cells))
    if table_mode == "html" or (table_mode == "hybrid" and has_spans):
        return render_html_table(norm, cells)
    norm = drop_empty_table_columns(norm)
    if not norm:
        return []
    header_source = norm[0]
    if looks_like_header_row(header_source):
        header = [escape_table_cell(c) or f"열{i+1}" for i, c in enumerate(header_source)]
        body = norm[1:] if len(norm) > 1 else [[""] * len(header)]
    else:
        header = [f"열{i+1}" for i in range(len(header_source))]
        body = norm
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    for row in body:
        out.append("| " + " | ".join(escape_table_cell(c) for c in row) + " |")
    return out


def drop_empty_table_columns(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    col_count = max(len(row) for row in rows)
    keep = [idx for idx in range(col_count) if any(idx < len(row) and row[idx].strip() for row in rows)]
    return [[(row[idx] if idx < len(row) else "") for idx in keep] for row in rows]


def looks_like_header_row(row: list[str]) -> bool:
    non_empty = [cell.strip() for cell in row if cell.strip()]
    if not non_empty:
        return False
    marker_pattern = r"^(?:[-*+]|[•·∙◦ㆍ□○■▶▷◇◆※]|\(?\d{1,4}\)?[.)]|\d{4}\.\d{1,2}\.\d{1,2})"
    if any(re.match(marker_pattern, cell) for cell in non_empty):
        return False
    numeric_like = sum(1 for cell in non_empty if re.fullmatch(r"[\d,.\-+%~\s]+", cell))
    if numeric_like >= max(1, len(non_empty) // 2):
        return False
    avg_len = sum(len(cell) for cell in non_empty) / len(non_empty)
    return avg_len <= 30


def render_html_table(rows: list[list[str]], cells: Optional[list[TableCell]] = None) -> list[str]:
    out = ["<table>"]
    if cells:
        max_row = max((cell.row + cell.rowspan for cell in cells), default=len(rows))
        for ridx in range(max_row):
            row_cells = sorted([cell for cell in cells if cell.row == ridx], key=lambda item: item.col)
            if not row_cells:
                continue
            parts = []
            for cell in row_cells:
                tag = "th" if ridx == 0 else "td"
                attrs = []
                if cell.rowspan > 1:
                    attrs.append(f'rowspan="{cell.rowspan}"')
                if cell.colspan > 1:
                    attrs.append(f'colspan="{cell.colspan}"')
                attr_text = (" " + " ".join(attrs)) if attrs else ""
                content = "<br>".join(html.escape(line) for line in format_table_cell_lines(cell.text))
                parts.append(f"<{tag}{attr_text}>{content}</{tag}>")
            out.append("  <tr>" + "".join(parts) + "</tr>")
    else:
        for ridx, row in enumerate(rows):
            tag = "th" if ridx == 0 else "td"
            out.append("  <tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in row) + "</tr>")
    out.append("</table>")
    return out


def escape_table_cell(text: str) -> str:
    return escape_md_text(text.replace("|", "\\|").replace("\n", "<br>"))


def convert_file(input_path: Path, output_path: Path, assets_dir: Optional[Path], report_path: Optional[Path], options: ConvertOptions) -> ConvertResult:
    start = time.time()
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if assets_dir is None:
        assets_dir = output_path.with_suffix(".assets")
    else:
        assets_dir = assets_dir.expanduser().resolve()
    if report_path is None:
        report_path = output_path.with_suffix(".report.json")
    else:
        report_path = report_path.expanduser().resolve()

    ensure_parent(output_path)
    ensure_parent(report_path)
    fmt = detect_format(input_path)
    if fmt == "hwp":
        result = convert_hwp(input_path, output_path, assets_dir, options)
    elif fmt == "hwpx":
        result = convert_hwpx(input_path, output_path, assets_dir, options)
    else:
        raise ConversionError(f"지원하지 않는 파일 형식입니다: {fmt}")

    result.report["elapsedSeconds"] = round(time.time() - start, 3)
    output_path.write_text(result.markdown, encoding="utf-8")
    report_path.write_text(json.dumps(result.report, ensure_ascii=False, indent=2), encoding="utf-8")
    if options.dump_ir:
        ir_path = output_path.with_suffix(".ir.json")
        ir = [dataclasses.asdict(b) for b in result.blocks]
        ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    if options.strict and (result.report.get("warnings") or result.report.get("losses")):
        raise ConversionError("strict 모드에서 warning/loss 항목이 발생했습니다. report.json을 확인하세요.")
    return result


def expand_batch(pattern: str) -> list[Path]:
    # Windows에서도 따옴표로 전달된 glob을 자체 확장한다.
    p = Path(pattern)
    if any(ch in pattern for ch in "*?["):
        base = p.parent if str(p.parent) not in {"", "."} else Path.cwd()
        matched = sorted(base.glob(p.name))
        return [x for x in matched if x.is_file()]
    return [p]


def unique_path(path: Path) -> Path:
    """이미 존재하는 경로와 충돌하지 않는 파일 경로를 만든다."""
    if not path.exists():
        return path
    for idx in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ConversionError(f"출력 파일명 충돌을 해결할 수 없습니다: {path}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HWP/HWPX 문서를 Markdown과 assets/report 파일로 변환합니다.")
    parser.add_argument("input", nargs="?", help="입력 .hwp 또는 .hwpx 파일")
    parser.add_argument("-o", "--output", help="출력 Markdown 경로")
    parser.add_argument("--assets-dir", help="이미지/첨부 자산 폴더")
    parser.add_argument("--format", choices=["gfm", "commonmark", "obsidian"], default="gfm", help="Markdown 출력 방언")
    parser.add_argument("--heading-policy", choices=["style", "outline", "font", "none"], default="font", help="제목 추론 정책")
    parser.add_argument("--table-mode", choices=["gfm", "html", "hybrid"], default="hybrid", help="표 출력 방식")
    parser.add_argument("--image-mode", choices=["extract", "embed", "skip"], default="extract", help="이미지 처리 방식. embed는 v0.1.0에서 extract와 동일하게 동작")
    parser.add_argument("--equation-mode", choices=["latex", "image", "both", "skip"], default="latex", help="수식 처리 방식. v0.1.0에서는 원문 보존 중심")
    parser.add_argument("--frontmatter", choices=["none", "yaml"], default="yaml", help="YAML frontmatter 출력 여부")
    parser.add_argument("--include-page-artifacts", action="store_true", help="머리말/꼬리말 등 페이지 부속 정보를 가능한 경우 포함")
    parser.add_argument("--no-clean-assets", action="store_true", help="재변환 시 기존 generated assets 정리를 건너뜁니다.")
    parser.add_argument("--report", help="변환 리포트 JSON 경로")
    parser.add_argument("--strict", action="store_true", help="손실/경고가 있으면 실패 처리")
    parser.add_argument("--batch", help="일괄 변환 glob 패턴. 예: \"samples/*.hwp\"")
    parser.add_argument("--dump-ir", action="store_true", help="개발자용 중간 IR JSON을 함께 출력")
    parser.add_argument("--version", action="version", version=f"hwp2md {VERSION}")
    args = parser.parse_args(argv)

    options = ConvertOptions(
        markdown_format=args.format,
        heading_policy=args.heading_policy,
        table_mode=args.table_mode,
        image_mode=args.image_mode,
        equation_mode=args.equation_mode,
        frontmatter=args.frontmatter,
        include_page_artifacts=args.include_page_artifacts,
        clean_assets=not args.no_clean_assets,
        strict=args.strict,
        dump_ir=args.dump_ir,
    )

    try:
        if args.batch:
            inputs = expand_batch(args.batch)
            if not inputs:
                raise ConversionError(f"batch 패턴에 매칭되는 파일이 없습니다: {args.batch}")
            out_dir = Path(args.output) if args.output else Path.cwd() / "converted"
            out_dir.mkdir(parents=True, exist_ok=True)
            ok = 0
            failed = 0
            for input_path in inputs:
                try:
                    md_path = out_dir / f"{source_stem(input_path)}.md"
                    assets_dir = Path(args.assets_dir) / source_stem(input_path) if args.assets_dir else out_dir / f"{md_path.stem}.assets"
                    report_path = md_path.with_suffix(".report.json") if not args.report else Path(args.report)
                    convert_file(input_path, md_path, assets_dir, report_path, options)
                    print(f"OK  {input_path} -> {md_path}")
                    ok += 1
                except Exception as exc:
                    print(f"FAIL {input_path}: {exc}", file=sys.stderr)
                    failed += 1
            print(f"batch done: ok={ok}, failed={failed}")
            return 1 if failed else 0

        if not args.input:
            parser.error("input 파일 또는 --batch 패턴이 필요합니다.")
        input_path = Path(args.input)
        output_path = Path(args.output) if args.output else input_path.with_suffix(".md")
        assets_dir = Path(args.assets_dir) if args.assets_dir else None
        report_path = Path(args.report) if args.report else None
        result = convert_file(input_path, output_path, assets_dir, report_path, options)
        print(f"Markdown: {output_path}")
        print(f"Report:   {report_path or output_path.with_suffix('.report.json')}")
        if result.assets:
            print(f"Assets:   {assets_dir or output_path.with_suffix('.assets')} ({len(result.assets)} files)")
        if result.report.get("warnings"):
            print(f"Warnings: {len(result.report['warnings'])}개 - report.json을 확인하세요.")
        return 0
    except ConversionError as exc:
        print(f"hwp2md 오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자 중단", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
