#!/usr/bin/env python3
"""
hwp2md: HWP/HWPX to Markdown converter

이 도구는 HWP 5.x OLE 문서와 HWPX ZIP/XML 문서를 Markdown 초안으로 변환한다.
목표는 픽셀 단위 레이아웃 재현이 아니라, 사람이 읽고 편집할 수 있는 의미 중심 Markdown 생성이다.
"""
from __future__ import annotations

import argparse
import base64
import binascii
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
ASSET_MANIFEST_NAME = ".hwp2md-assets.json"
PAGE_ARTIFACT_MAX_ROWS = 8
NON_CONTENT_BLOCK_TYPES = frozenset({"metadata", "page_artifact"})
CORRUPT_FILE_ERRORS = (zipfile.BadZipFile, zlib.error, struct.error, ValueError, KeyError, EOFError, IndexError, ET.ParseError, RecursionError)
# 압축 폭탄 대비 한도. 실제 문서 11,595개에서 본 최댓값은 섹션 34MB, 그림 하나 47MB, 문서 한 개의 그림 합계 595MB였다.
MAX_SECTION_BYTES = 50 * 1024 * 1024
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 2 * 1024 * 1024 * 1024
HWP_TEXT_LEAD_BYTES = b"\xef\xbb\xbf \t\r\n"
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
# 컨트롤 ID는 리틀 엔디언으로 저장되어 "fn  "은 b"  nf", "en  "(미주)은 b"  ne"로 나타난다.
HWP_FOOTNOTE_CTRL_IDS = {b"  nf", b"  ne"}
# 머리말("head")/꼬리말("foot")도 같은 이유로 b"daeh"/b"toof"이다.
HWP_HEADER_FOOTER_CTRL_IDS = {b"daeh", b"toof"}
HWP_CELL_FOOTNOTE_OPEN = "(각주: "
HWP_LAYOUT_CTRL_IDS = {b"dces", b"dloc", b"pngp", b"dhgp", b"onwn", b"spct", b"tcgp", b"daeh", b"toof"}
HWP_FIELD_CTRL_IDS = {b"umf%", b"klc%"}
HWP_PRIVATE_SQUARE_BULLET = "\U000f03da"
HWP_PRIVATE_NUMBER_BASE = 0xF02B0
# Wingdings/Symbol 글꼴 기호는 U+F000 + 글꼴 코드 위치의 사용자 정의 영역 글자로 저장된다.
# 그대로 두면 뷰어에서 네모로 보이므로 대응하는 표준 유니코드 기호로 바꾼다.
HWP_SYMBOL_FONT_MAP = {
    0xF06C: "●", 0xF06D: "❍", 0xF06E: "■", 0xF06F: "□", 0xF071: "❑",
    0xF075: "◆", 0xF076: "❖", 0xF077: "◆", 0xF09E: "·", 0xF09F: "•",
    0xF0A7: "▪", 0xF0D8: "➢", 0xF0E8: "➔", 0xF0F0: "⇨", 0xF0FC: "✓",
    0xF081: "①", 0xF082: "②", 0xF083: "③", 0xF084: "④", 0xF085: "⑤",
    0xF086: "⑥", 0xF087: "⑦", 0xF088: "⑧", 0xF089: "⑨", 0xF08A: "⑩",
    0xF53A: "한",  # 한글 로고 글자
    # 아래는 실제 문서의 문맥으로 용도를 확인한 항목이다: 단어 사이 가운뎃점(속기록·녹음), 문단 앞 불릿, 전화, 체크박스.
    0xF85E: "·", 0xF0A0: "·", 0xF073: "◆", 0xF0A1: "○", 0xF028: "☎", 0xF0FE: "☑",
}


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
    number: int | None = None


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


def unsupported_format_reason(head: bytes) -> Optional[str]:
    """OLE/ZIP이 아닌 파일의 헤더를 보고 변환할 수 없는 이유를 알려준다."""
    if head.startswith(b"HWP Document File"):
        return "HWP 3.0(한글 97 이전) 형식이라 지원하지 않습니다. 한글에서 열어 .hwp(5.x) 또는 .hwpx로 다시 저장한 뒤 변환하세요."
    if b"DRMONE" in head[:16]:
        return "DRM으로 암호화된 문서라 변환할 수 없습니다. DRM을 해제한 파일로 다시 시도하세요."
    stripped = head.lstrip(HWP_TEXT_LEAD_BYTES)
    if stripped.startswith(b"<"):
        return "XML/HTML 텍스트 문서이며 HWP 파일이 아닙니다. 확장자만 .hwp로 저장된 파일일 수 있습니다."
    return None


def detect_format(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(32)
    if not head:
        raise ConversionError("빈 파일(0바이트)입니다. 내용이 없는 파일은 변환할 수 없습니다.")
    suffix = path.suffix.lower()
    if head.startswith(HWP_CFB_SIGNATURE):
        return "hwp"
    if zipfile.is_zipfile(path):
        return "hwpx"
    if head.lstrip(HWP_TEXT_LEAD_BYTES).startswith(b"<"):
        # 법제처 등에서 내려받는 .hwp 중에는 한글 XML 형식(HWPML)으로 저장된 파일이 많다.
        with path.open("rb") as f:
            if b"<HWPML" in f.read(4096):
                return "hwpml"
    reason = unsupported_format_reason(head)
    if reason:
        raise ConversionError(reason)
    if suffix == ".hwp":
        return "hwp"
    if suffix == ".hwpx":
        return "hwpx"
    if suffix == ".hml":
        return "hwpml"
    raise ConversionError(f"지원하지 않는 파일 형식입니다: {path}")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def safe_stem(path: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", path.stem).strip("._-")
    return stem or "document"


def source_stem(path: Path) -> str:
    stem = path.stem.strip()
    return stem or safe_stem(path)


INLINE_MATH_RE = re.compile(r"(\\\(.*?\\\))", re.S)


def escape_md_text(text: str) -> str:
    # 문장 내부의 과도한 이스케이프는 가독성을 해치므로 Markdown 구조 문자 일부만 보수적으로 처리한다.
    # 수식(\( ... \))은 LaTeX 그대로 두어야 하므로 이스케이프하지 않는다.
    pieces = INLINE_MATH_RE.split(text)
    for idx in range(0, len(pieces), 2):
        pieces[idx] = re.sub(r"([*_`])", r"\\\1", pieces[idx].replace("\\", "\\\\"))
    text = "".join(pieces)
    # 줄 맨 앞의 '#', '>'는 제목/인용으로 해석되므로 이스케이프한다.
    text = re.sub(r"^(\s*)([#>])", r"\1\\\2", text)
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


HWP_EXTENDED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x17]")


# 하이퍼링크 위치를 글자 정리 과정 동안 보존하는 표식. 유니코드 비문자라 본문 글자와 겹치지 않는다.
LINK_OPEN, LINK_SEP, LINK_CLOSE, LINK_CLOSE_END = "﷐", "﷑", "﷒", "﷓"
LINK_MARK_RE = re.compile("﷐(\\d+)﷑([^﷑]*)﷑(.*?)﷒\\1﷓", re.S)
LINK_LEFTOVER_RE = re.compile("﷐\\d*﷑[^﷑]*﷑|﷒\\d*﷓|[﷐-﷓]")
HWP_HYPERLINK_CTRL_ID = b"klh%"


def link_open_mark(link_id: str, url: str) -> str:
    return f"{LINK_OPEN}{link_id}{LINK_SEP}{url}{LINK_SEP}"


def link_close_mark(link_id: str) -> str:
    return f"{LINK_CLOSE}{link_id}{LINK_CLOSE_END}"


def finalize_link_marks(text: str) -> str:
    """짝이 맞는 링크 표식을 [글자](주소)로 바꾸고 짝 없는 표식은 지운다."""
    if LINK_OPEN not in text and LINK_CLOSE not in text:
        return text

    def to_link(match: re.Match[str]) -> str:
        url, label = match.group(2), finalize_link_marks(match.group(3))
        label = re.sub(r"\s*\n\s*", " ", label)
        return f"[{label}]({url})" if url and label.strip() else label

    return LINK_LEFTOVER_RE.sub("", LINK_MARK_RE.sub(to_link, text))


def hyperlink_url(command: str) -> str:
    """한글 하이퍼링크 명령("주소;종류;...;", 역슬래시 이스케이프)에서 웹 주소만 꺼낸다. 문서 안 책갈피 등은 빈 문자열."""
    match = re.match(r"(?:\\.|[^;\\])*", command)
    target = re.sub(r"\\(.)", r"\1", match.group(0) if match else "").strip()
    if not re.match(r"(?i)(?:https?|ftp|mailto):|www\.", target):
        return ""
    if target.lower().startswith("www."):
        target = "http://" + target
    return target.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def parse_hwp_hyperlink_command(payload: bytes) -> str:
    # 필드 컨트롤: id(4) 속성(4) 기타 속성(1) 명령 글자 수(WORD) 명령(WCHAR)
    if len(payload) < 11:
        return ""
    length = struct.unpack_from("<H", payload, 9)[0]
    return payload[11 : 11 + 2 * length].decode("utf-16le", errors="ignore")


HWP_AUTO_NUMBER_CTRL_ID = b"onta"
# 자동 번호 중 그림·표·수식 캡션 번호만 본문에 채운다. 쪽 번호는 저장 당시 값일 뿐이고 각주 번호는 Markdown이 다시 매긴다.
CAPTION_NUMBER_TYPES = {"PICTURE", "FIGURE", "TABLE", "EQUATION"}
HWP_AUTO_NUMBER_TYPES = {3: "PICTURE", 4: "TABLE", 5: "EQUATION"}
AUTO_NUMBER_SHAPES = {"DIGIT": 0, "CIRCLEDDIGIT": 1, "ROMANCAPITAL": 2, "ROMANSMALL": 3, "LATINCAPITAL": 4, "LATINSMALL": 5}


def format_auto_number(number: int, shape: int) -> str:
    if shape == 1 and 1 <= number <= 20:
        return chr(0x2460 + number - 1)
    if shape in (2, 3) and 0 < number < 4000:
        roman = ""
        for value, letters in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            count, number = divmod(number, value)
            roman += letters * count
        return roman if shape == 2 else roman.lower()
    if shape in (4, 5) and 1 <= number <= 26:
        return chr((0x41 if shape == 4 else 0x61) + number - 1)
    return str(number)


def parse_hwp_auto_number(payload: bytes) -> str:
    # 자동 번호 컨트롤: id(4) 속성(UINT32: 0~3비트 종류, 4~11비트 모양) 번호(WORD) 사용자 기호·앞 장식·뒤 장식(WCHAR)
    if len(payload) < 10:
        return ""
    prop, number = struct.unpack_from("<IH", payload, 4)
    if HWP_AUTO_NUMBER_TYPES.get(prop & 0x0F) is None:
        return ""
    prefix, suffix = struct.unpack_from("<HH", payload, 12) if len(payload) >= 16 else (0, 0)
    decorate = lambda code: chr(code) if code >= 32 else ""  # noqa: E731
    return f"{decorate(prefix)}{format_auto_number(number, (prop >> 4) & 0xFF)}{decorate(suffix)}"


def hwp_paragraph_controls(records: list[tuple[int, int, bytes]], text_idx: int) -> Optional[dict[bytes, list[str]]]:
    """PARA_TEXT 뒤에 나오는 같은 문단의 하이퍼링크 주소와 자동 번호를 순서대로 모은다."""
    payload = records[text_idx][2]
    if HWP_HYPERLINK_CTRL_ID not in payload and HWP_AUTO_NUMBER_CTRL_ID not in payload:
        return None
    text_level = records[text_idx][1]
    controls: dict[bytes, list[str]] = {HWP_HYPERLINK_CTRL_ID: [], HWP_AUTO_NUMBER_CTRL_ID: []}
    for tag, level, ctrl_payload in records[text_idx + 1 :]:
        if level < text_level:
            break
        if level != text_level or tag != HWP_CTRL_HEADER_TAG:
            continue
        if ctrl_payload[:4] == HWP_HYPERLINK_CTRL_ID:
            controls[HWP_HYPERLINK_CTRL_ID].append(hyperlink_url(parse_hwp_hyperlink_command(ctrl_payload)))
        elif ctrl_payload[:4] == HWP_AUTO_NUMBER_CTRL_ID:
            controls[HWP_AUTO_NUMBER_CTRL_ID].append(parse_hwp_auto_number(ctrl_payload))
    return controls


def clean_hwp_text_payload(payload: bytes, controls: Optional[dict[bytes, list[str]]] = None) -> str:
    """HWP PARA_TEXT 레코드에서 사람이 읽을 수 있는 문자열을 최대한 보수적으로 추출한다.

    HWP 바이너리 텍스트에는 문단/표/그림/필드 제어 문자가 UTF-16 코드 단위 사이에
    삽입된다. 일부 inline 제어문자는 바로 뒤의 숫자/기호와 붙어 나타나므로, 구조적
    제거 결과가 본문을 잃었다고 판단되는 경우에는 제어문자 자체만 걷어낸 결과를 쓴다.
    """
    s = payload.decode("utf-16le", errors="ignore")
    if not HWP_EXTENDED_CONTROL_RE.search(s):
        # 확장 제어문자가 없으면 두 방식의 결과가 같으므로 한 번만 계산한다(대부분의 문단).
        return clean_hwp_decoded_text(s, skip_extended_controls=True)
    structured = clean_hwp_decoded_text(s, skip_extended_controls=True)
    visible = clean_hwp_decoded_text(s, skip_extended_controls=False)
    if should_use_visible_hwp_text(structured, visible):
        return visible
    if controls:
        return clean_hwp_decoded_text(s, skip_extended_controls=True, controls=controls)
    return structured


def clean_hwp_decoded_text(s: str, skip_extended_controls: bool, controls: Optional[dict[bytes, list[str]]] = None) -> str:
    out: list[str] = []
    links = controls.get(HWP_HYPERLINK_CTRL_ID) if controls else None
    pending_links = iter(links or [])
    pending_numbers = iter(controls.get(HWP_AUTO_NUMBER_CTRL_ID) or []) if controls else iter(())
    # 필드 시작(3)과 끝(4)은 짝을 이루며 중첩될 수 있다. 하이퍼링크 필드만 링크 번호를 기억한다.
    open_fields: list[Optional[str]] = []
    i = 0
    while i < len(s):
        ch = s[i]
        code = ord(ch)
        if controls and code == 18 and s[i + 1 : i + 3] == "湯慴":  # 자동 번호 컨트롤 ID "atno"
            out.append(next(pending_numbers, ""))
        if links is not None and code == 3:
            is_link = s[i + 1 : i + 3] == "汫╨"  # 컨트롤 ID "%hlk"
            url = next(pending_links, "") if is_link else ""
            open_fields.append(str(i) if url else None)
            if url:
                out.append(link_open_mark(str(i), url))
        elif links is not None and code == 4 and open_fields:
            link_id = open_fields.pop()
            if link_id is not None:
                out.append(link_close_mark(link_id))
        if ch in "\r\n\t":
            out.append("\n" if ch in "\r\n" else "\t")
            # 탭(9)은 인라인 컨트롤이라 [9, 너비, ..., 9] 총 8유닛이다. 뒤 7유닛은 글자가 아니므로 건너뛴다.
            i += 8 if ch == "\t" and i + 7 < len(s) and s[i + 7] == "\t" else 1
            continue
        if 24 <= code <= 31:
            # 24~31은 뒤에 데이터가 붙지 않는 1유닛 문자 제어문자다(24 하이픈, 30/31 묶음·고정폭 빈칸).
            # 확장 제어문자처럼 건너뛰면 뒤따르는 본문 글자를 삼키므로 문자 하나만 처리한다.
            if code == 24:
                out.append("-")
            elif code in (30, 31):
                out.append(" ")
            i += 1
            continue
        if code < 32:
            # 확장 제어문자는 대체로 control char + 4 UTF-16 units + control char 형태로 나타난다.
            if not skip_extended_controls:
                i += 1
            elif i + 7 < len(s) and s[i + 7] == ch:
                i += 8
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
    text = "".join(out).translate(HWP_SYMBOL_FONT_MAP)
    # HWP 내부 객체 anchor가 남긴 짧은 영문 토큰 제거.
    text = re.sub(r"\b(?:dces|lcol|hdpg|pnpg|onwn|tcpg| lbt|dahe)\b", "", text)
    return finalize_link_marks(clean_plain_text(text))


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
    caption_lines: list[str] = []
    idx = start_idx + 1
    saw_table_record = False

    while idx < len(records):
        rec_tag, rec_level, rec_payload = records[idx]
        if idx > start_idx + 1 and rec_level <= table_level:
            break

        # 셀 안에 중첩된 표의 TABLE/LIST_HEADER 레코드는 바깥 표의 구조를 덮어쓰면 안 된다.
        # 중첩 표의 문단 텍스트는 아래 PARA_TEXT 분기에서 현재 셀의 텍스트로 합쳐진다.
        is_direct_child = rec_level == table_level + 1
        if rec_tag == HWP_TABLE_TAG and is_direct_child:
            dims = parse_hwp_table_dimensions(rec_payload)
            if dims:
                rows, cols = dims
                saw_table_record = True
        elif rec_tag == HWP_TABLE_CELL_TAG and rows and cols and is_direct_child:
            parsed_cell = parse_hwp_table_cell_header(rec_payload, rows, cols)
            if parsed_cell:
                current_cell = parsed_cell
                cells.append(current_cell)
        elif current_cell is not None and rec_tag == HWP_CTRL_HEADER_TAG and rec_level > table_level + 1:
            control_id = rec_payload[:4]
            addition: Optional[str] = None
            if control_id in HWP_FOOTNOTE_CTRL_IDS:
                footnote_text, next_idx = extract_hwp_footnote(records, idx)
                if footnote_text:
                    current_cell.text = f"{current_cell.text} {HWP_CELL_FOOTNOTE_OPEN}{footnote_text})".strip()
                idx = next_idx
                continue
            if control_id in HWP_EQUATION_CTRL_IDS:
                equation = extract_hwp_equation(records, idx) or r"\text{HWP equation}"
                addition = rf"\({equation}\)"
            if addition:
                current_cell.text = "\n".join(part for part in [current_cell.text, addition] if part)
        elif current_cell is not None and rec_tag == HWP_PARA_TEXT_TAG and rec_level > table_level + 1:
            text = clean_hwp_text_payload(rec_payload, hwp_paragraph_controls(records, idx))
            if text:
                current_cell.text = "\n".join(part for part in [current_cell.text, text] if part)
        elif current_cell is None and not saw_table_record and rec_tag == HWP_PARA_TEXT_TAG and rec_level > table_level + 1:
            # TABLE 레코드보다 앞서 나오는 문단 리스트는 표 캡션이다. 셀이 아니므로 따로 모았다가 표 앞에 낸다.
            caption = clean_hwp_text_payload(rec_payload, hwp_paragraph_controls(records, idx))
            if caption:
                caption_lines.append(caption)

        idx += 1

    if not saw_table_record or rows is None or cols is None or not cells:
        return None

    blocks = finalize_table_blocks(rows, cols, cells, caption_lines, heading_policy, document_has_blocks, include_page_artifacts)
    return blocks, idx


def finalize_table_blocks(
    rows: int,
    cols: int,
    cells: list[TableCell],
    caption_lines: list[str],
    heading_policy: str,
    document_has_blocks: bool,
    include_page_artifacts: bool,
) -> list[Block]:
    """복원한 표(HWP/HWPX 공용)를 보도자료 머리표/결재란/표/문단 중 알맞은 블록으로 바꾼다."""
    grid = build_hwp_table_grid(rows, cols, cells)
    table_text = " ".join(cell.text for cell in cells if cell.text).strip()
    caption_blocks: list[Block] = []
    for caption in caption_lines:
        for part in split_text_to_candidate_blocks(caption):
            append_text_block(caption_blocks, part, heading_policy, is_first=not document_has_blocks and not caption_blocks, include_page_artifacts=include_page_artifacts)
    if is_press_release_header_table(table_text) and not include_page_artifacts:
        metadata = parse_press_release_metadata(cells)
        return [Block(type="metadata", text=json.dumps(metadata, ensure_ascii=False))] if metadata else []
    if not table_text:
        # 칸이 전부 비어 있는 표는 버리되, 표 위/아래 캡션에 글이 있으면 그 글은 남긴다.
        return caption_blocks
    if is_page_artifact_table(rows, cols, cells, grid) and not include_page_artifacts:
        dropped = " ".join(table_text.split())
        return caption_blocks + [Block(type="page_artifact", text=dropped)]
    if should_render_hwp_table(rows, cols, cells, grid):
        return caption_blocks + [Block(type="table", rows=grid, cells=cells)]

    flattened: list[Block] = list(caption_blocks)
    for cell in sorted(cells, key=lambda item: (item.row, item.col)):
        for part in split_text_to_candidate_blocks(cell.text):
            is_first = not document_has_blocks and not flattened
            # 표 칸의 숫자는 쪽 번호가 아니라 데이터이므로 쪽 번호 필터를 적용하지 않는다.
            append_text_block(flattened, part, heading_policy, is_first=is_first, include_page_artifacts=True)
    return flattened


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
            match = re.match(r"^(?:(?:[•·∙◦ㆍ□○〇■▶▷◇◆※ㅇ●❍❑❖▪➔⇨➢✓])\s*|(?:[-*+])\s+)(?P<body>.+)$", line)
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
    # 수신자/제목이 들어 있는 공문 머리 표는 결재란과 한 표로 묶여 있어도 본문 정보이므로 보존한다.
    if "수신자" in text or re.search(r"제\s*목", text):
        return False
    # 결재란/시행란은 행 수가 적다. 행이 많은 큰 표는 신청서 같은 서식 본문이므로 버리지 않는다.
    if cols < 12 or rows > PAGE_ARTIFACT_MAX_ROWS:
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
    notes: list[str] = []
    for _, row_cells in sorted(rows.items()):
        ordered = sorted(row_cells, key=lambda item: item.col)
        texts = [clean_plain_text(cell.text.replace("\n", " ")) for cell in ordered]
        texts = [text for text in texts if text]
        used: set[int] = set()
        for idx, text in enumerate(texts):
            label = re.sub(r"\s+", "", text)
            inline = None if label in PRESS_LABEL_WORDS else PRESS_INLINE_LABEL_RE.match(text)
            if label in PRESS_LABEL_WORDS:
                used.add(idx)
            if inline:
                # "배포일시 2012.8.28(화)"처럼 항목 이름과 값이 한 칸에 같이 있는 경우
                label, value_index, value = re.sub(r"\s+", "", inline.group(1)), idx, inline.group(2)
            else:
                value_index = next_metadata_value_index(texts, idx)
                value = texts[value_index] if value_index is not None else None
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
            else:
                continue
            used.add(value_index)
        # 위 항목으로 옮기지 못한 칸(담당과장, 둘째 이후 담당 부서, 엠바고 안내 등)은 버리지 않고 남긴다.
        leftover = " ".join(text for idx, text in enumerate(texts) if idx not in used)
        if leftover:
            notes.append(leftover)

    if "contact" in metadata:
        phones = normalize_phone_numbers(metadata["contact"])
        if phones:
            metadata["contact_phone"] = ", ".join(phones)
        contacts = parse_press_contacts(metadata["contact"], metadata.get("department"))
        if contacts:
            metadata["contacts"] = contacts
    if notes:
        metadata["notes"] = notes
    return metadata


PRESS_LABEL_WORDS = frozenset({"보도자료", "배포일시", "보도일시", "담당", "부서", "담당부서", "담당/부서", "담당자"})
PRESS_INLINE_LABEL_RE = re.compile(
    r"^(" + "|".join(r"\s*".join(word) for word in ("배포일시", "보도일시", "담당부서", "담당자")) + r")\s*[:：]?\s*(\S.*)$"
)


def next_metadata_value_index(texts: list[str], label_index: int) -> Optional[int]:
    for index in range(label_index + 1, len(texts)):
        if re.sub(r"\s+", "", texts[index]) in PRESS_LABEL_WORDS:
            continue
        return index
    return None


def next_metadata_value(texts: list[str], label_index: int) -> Optional[str]:
    index = next_metadata_value_index(texts, label_index)
    return texts[index] if index is not None else None


PRESS_ROLE_WORDS = frozenset({
    "과장", "국장", "실장", "팀장", "단장", "반장", "소장", "부장", "차장", "계장", "주임", "센터장", "담당관",
    "서기관", "사무관", "주무관", "행정관", "주사", "연구관", "연구사", "연구원", "전문관", "전문위원",
    "기술서기관", "기술사무관", "행정사무관", "검사", "부장검사",
    "조사관", "감사관", "심판관", "정책관", "기획관", "협력관", "대변인", "공보관", "서기", "위원",
})


def parse_press_contacts(text: str, department: Optional[str] = None) -> list[dict[str, str]]:
    cleaned = re.sub(r"[∙•]", " ", text)
    parts = re.split(r"☎|전화|Tel\.?", cleaned, maxsplit=1, flags=re.I)
    people_text = parts[0]
    # ☎ 표시 없이 "김영임 서기관(044-215-7111)"처럼 이름 옆에 번호를 쓴 칸도 있다.
    phones = normalize_phone_numbers(parts[1] if len(parts) > 1 else parts[0])
    people_text = re.sub(r"\S+@\S+|\([^)]*\)|[\d()~-]+", " ", people_text)
    for role in PRESS_ROLE_WORDS:
        # "과 장"처럼 글자 사이를 띄어 쓴 직급을 붙인다.
        people_text = re.sub(r"(?<![가-힣])" + r"\s+".join(role) + r"(?![가-힣])", role, people_text)
    people: list[dict[str, str]] = []
    last_role = ""
    for item in re.split(r"[,/·]", people_text):
        tokens = item.split()
        if not tokens:
            continue
        is_name = lambda token: token not in PRESS_ROLE_WORDS and re.fullmatch(r"[가-힣]{2,4}", token)
        pairs: list[tuple[str, str]] = []
        if any(token in PRESS_ROLE_WORDS for token in tokens):
            # 직급 목록으로 직급과 이름을 가리므로 "직급 이름"과 "이름 직급" 순서를 모두 읽는다.
            # 쉼표 없이 여러 사람을 이어 쓴 경우("과장 이원두 조사관 이효권")도 순서대로 짝짓는다.
            role_first = tokens[0] in PRESS_ROLE_WORDS
            pending: list[str] = []
            for token in tokens:
                if token in PRESS_ROLE_WORDS:
                    if not role_first:
                        pairs += [(token, name) for name in pending]
                        pending = []
                    last_role = token
                elif is_name(token):
                    if role_first:
                        pairs.append((last_role, token))
                    else:
                        pending.append(token)
            pairs += [(last_role, name) for name in pending]
        elif len(tokens) == 2 and re.fullmatch(r"[가-힣A-Za-z]+", tokens[0]) and is_name(tokens[1]):
            # 목록에 없는 직급은 예전처럼 "직급 이름" 순서로 본다.
            last_role = tokens[0]
            pairs.append((last_role, tokens[1]))
        elif len(tokens) == 1 and last_role and is_name(tokens[0]):
            pairs.append((last_role, tokens[0]))  # 직급 없이 이름 하나만 있으면 앞 사람의 직급을 잇는다.
        for role, name in pairs:
            contact = {"role": role, "name": name}
            if department:
                contact["department"] = department
            people.append(contact)
    # 번호가 하나면 모두가 같이 쓰는 번호이고, 사람 수와 같을 때만 순서대로 짝짓는다. 그 밖에는 짐작하지 않는다.
    if len(phones) == 1:
        for contact in people:
            contact["phone"] = phones[0]
    elif len(phones) == len(people):
        for contact, phone in zip(people, phones):
            contact["phone"] = phone
    return people


def normalize_phone_numbers(text: str) -> list[str]:
    if not text:
        return []
    # "201 - 3402"처럼 하이픈 앞뒤에 공백을 둔 번호가 조각나지 않도록 먼저 붙인다.
    text = re.sub(r"\s*-\s*", "-", text.strip())
    numbers = re.findall(r"(?:\(?0\d{1,3}\)?[-)\s]?)?\d{3,4}[-\s]?\d{4}|\b\d{3,4}\b", text)
    normalized: list[str] = []
    last_full = ""
    for number in numbers:
        number = re.sub(r"\s+", " ", number.strip())
        if re.fullmatch(r"\d{3,4}", number):
            if number.startswith("0"):
                continue  # 지역번호만 따로 떨어진 조각
            if last_full:
                # "201-3398, 3400"은 앞 번호의 끝자리만 바꿔 쓴 201-3400이다.
                number = last_full[: -len(number)] + number
        else:
            if number.startswith("(") and ")" not in number:
                number = number[1:]  # "서기관(044-215-7111)"에서 여는 괄호만 따라온 경우
            last_full = number
        normalized.append(number)
    return normalized


class DecompressLimitError(ValueError):
    pass


class ZipMemberError(Exception):
    pass


def format_megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f}MB"


def bounded_zlib_decompress(data: bytes, wbits: int, limit: int) -> bytes:
    # zlib.decompress와 같지만, 풀린 크기가 한도를 넘으면 끝까지 풀지 않고 멈춘다.
    decompressor = zlib.decompressobj(wbits)
    out = decompressor.decompress(data, limit + 1)
    if len(out) > limit:
        raise DecompressLimitError(f"압축을 푼 크기가 한도({format_megabytes(limit)})를 넘습니다")
    if not decompressor.eof:
        raise zlib.error("incomplete or truncated stream")
    return out


def read_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise ZipMemberError(f"압축을 푼 크기({format_megabytes(info.file_size)})가 한도({format_megabytes(limit)})를 넘습니다")
    if info.flag_bits & 0x1:
        raise ZipMemberError("암호가 걸린 항목입니다")
    try:
        # 헤더의 크기는 거짓일 수 있다. zipfile은 적힌 크기를 넘어서도 끝까지 풀기 때문에 실제로 읽는 양을 한도로 자른다.
        with zf.open(info) as fp:
            data = fp.read(limit + 1)
    except NotImplementedError as exc:
        raise ZipMemberError(f"지원하지 않는 압축 방식입니다 (method {info.compress_type})") from exc
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise ZipMemberError(f"손상된 항목입니다 ({type(exc).__name__}: {str(exc)[:80]})") from exc
    if len(data) > limit:
        raise ZipMemberError(f"압축을 푼 크기가 한도({format_megabytes(limit)})를 넘습니다")
    return data


def read_hwp_stream(ole: Any, stream: list[str] | str, compressed: bool, distributed: bool = False) -> bytes:
    data = ole.openstream(stream).read()
    if distributed:
        data = decrypt_hwp_view_text(data)
    if compressed:
        try:
            return bounded_zlib_decompress(data, -15, MAX_SECTION_BYTES)
        except DecompressLimitError:
            raise
        except zlib.error:
            return bounded_zlib_decompress(data, 15, MAX_SECTION_BYTES)
    return data


HWP_DISTRIBUTE_DOC_DATA_TAG = 28


def decrypt_hwp_view_text(data: bytes) -> bytes:
    """배포용 문서의 ViewText/Section 스트림을 AES-128(ECB)로 풀어 BodyText와 같은 형태로 돌려준다.

    스트림 맨 앞의 DISTRIBUTE_DOC_DATA 레코드(256바이트)에 복호화 키가 섞여 들어 있다.
    """
    if len(data) < 4 + 256:
        raise ConversionError("배포용 문서의 ViewText 스트림이 너무 짧습니다.")
    header = struct.unpack_from("<I", data, 0)[0]
    if header & 0x3FF != HWP_DISTRIBUTE_DOC_DATA_TAG or (header >> 20) & 0xFFF != 256:
        raise ConversionError("배포용 문서의 복호화 정보(DISTRIBUTE_DOC_DATA)를 찾을 수 없습니다.")
    key = hwp_distribution_key(data[4 : 4 + 256])
    body = data[4 + 256 :]
    return aes128_ecb_decrypt(key, body[: len(body) - len(body) % 16])


def hwp_distribution_key(payload: bytes) -> bytes:
    # 앞 4바이트를 씨앗으로 MSVC rand()를 돌려 나온 값으로 나머지를 XOR하면 키가 드러난다.
    seed = struct.unpack_from("<I", payload, 0)[0]
    data = bytearray(payload)
    state = seed
    value = count = 0
    for i in range(256):
        if count == 0:
            state = (state * 214013 + 2531011) & 0xFFFFFFFF
            value = (state >> 16) & 0xFF
            state = (state * 214013 + 2531011) & 0xFFFFFFFF
            count = ((state >> 16) & 0x0F) + 1
        if i >= 4:
            data[i] ^= value
        count -= 1
    offset = 4 + (seed & 0x0F)
    return bytes(data[offset : offset + 16])


_AES_TABLES: Optional[tuple[list[int], list[int], list[list[int]]]] = None


def aes_tables() -> tuple[list[int], list[int], list[list[int]]]:
    """S-box, 역 S-box, 복호화용 T-table 4개를 처음 쓸 때 한 번만 계산한다."""
    global _AES_TABLES
    if _AES_TABLES is not None:
        return _AES_TABLES

    def gmul(a: int, b: int) -> int:
        out = 0
        while b:
            if b & 1:
                out ^= a
            a = ((a << 1) ^ 0x11B) if a & 0x80 else a << 1
            b >>= 1
        return out

    def rotl8(x: int, shift: int) -> int:
        return ((x << shift) | (x >> (8 - shift))) & 0xFF

    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        sbox[p] = q ^ rotl8(q, 1) ^ rotl8(q, 2) ^ rotl8(q, 3) ^ rotl8(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv_sbox = [0] * 256
    for i, s in enumerate(sbox):
        inv_sbox[s] = i
    td0 = [(gmul(s, 14) << 24) | (gmul(s, 9) << 16) | (gmul(s, 13) << 8) | gmul(s, 11) for s in inv_sbox]
    tds = [td0]
    for shift in (8, 16, 24):
        tds.append([((x >> shift) | (x << (32 - shift))) & 0xFFFFFFFF for x in td0])
    _AES_TABLES = (sbox, inv_sbox, tds)
    return _AES_TABLES


def aes128_decrypt_round_keys(key: bytes) -> list[int]:
    sbox, _inv_sbox, (td0, td1, td2, td3) = aes_tables()
    words = list(struct.unpack(">4I", key))
    rcon = 1
    for i in range(4, 44):
        t = words[i - 1]
        if i % 4 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = (sbox[t >> 24] << 24) | (sbox[(t >> 16) & 0xFF] << 16) | (sbox[(t >> 8) & 0xFF] << 8) | sbox[t & 0xFF]
            t ^= rcon << 24
            rcon = ((rcon << 1) ^ 0x11B) if rcon & 0x80 else rcon << 1
        words.append(words[i - 4] ^ t)
    # 복호화는 라운드 키를 거꾸로 쓰고, 처음과 마지막을 뺀 라운드 키에는 InvMixColumns를 미리 적용한다.
    round_keys: list[int] = []
    for rnd in range(10, -1, -1):
        for w in words[rnd * 4 : rnd * 4 + 4]:
            if 0 < rnd < 10:
                w = td0[sbox[w >> 24]] ^ td1[sbox[(w >> 16) & 0xFF]] ^ td2[sbox[(w >> 8) & 0xFF]] ^ td3[sbox[w & 0xFF]]
            round_keys.append(w)
    return round_keys


def aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    _sbox, si, (td0, td1, td2, td3) = aes_tables()
    rk = aes128_decrypt_round_keys(key)
    out = bytearray(len(data))
    for pos in range(0, len(data), 16):
        s0, s1, s2, s3 = struct.unpack_from(">4I", data, pos)
        s0 ^= rk[0]
        s1 ^= rk[1]
        s2 ^= rk[2]
        s3 ^= rk[3]
        for r in range(4, 40, 4):
            t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ rk[r]
            t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ rk[r + 1]
            t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ rk[r + 2]
            t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ rk[r + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
        struct.pack_into(
            ">4I",
            out,
            pos,
            ((si[s0 >> 24] << 24) | (si[(s3 >> 16) & 0xFF] << 16) | (si[(s2 >> 8) & 0xFF] << 8) | si[s1 & 0xFF]) ^ rk[40],
            ((si[s1 >> 24] << 24) | (si[(s0 >> 16) & 0xFF] << 16) | (si[(s3 >> 8) & 0xFF] << 8) | si[s2 & 0xFF]) ^ rk[41],
            ((si[s2 >> 24] << 24) | (si[(s1 >> 16) & 0xFF] << 16) | (si[(s0 >> 8) & 0xFF] << 8) | si[s3 & 0xFF]) ^ rk[42],
            ((si[s3 >> 24] << 24) | (si[(s2 >> 16) & 0xFF] << 16) | (si[(s1 >> 8) & 0xFF] << 8) | si[s0 & 0xFF]) ^ rk[43],
        )
    return bytes(out)


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
    footnote_restored_count: int = 0,
) -> None:
    non_table = {key: value for key, value in control_counts.items() if key != HWP_TABLE_CTRL_ID}
    if not non_table:
        return
    equation_count = sum(count for key, count in non_table.items() if key in HWP_EQUATION_CTRL_IDS)
    footnote_count = sum(count for key, count in non_table.items() if key in HWP_FOOTNOTE_CTRL_IDS)
    equation_dropped = max(0, equation_count - equation_fallback_count)
    footnote_dropped = max(0, footnote_count - footnote_fallback_count - footnote_restored_count)
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
    legacy_pattern = re.compile(re.escape(stem) + r"-\d{3}\.[0-9A-Za-z]+")
    manifest_names = read_asset_manifest(assets_dir)
    for path in assets_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in manifest_names or legacy_pattern.fullmatch(path.name):
            path.unlink()
            removed += 1
    return removed


def read_asset_manifest(assets_dir: Path) -> set[str]:
    try:
        data = json.loads((assets_dir / ASSET_MANIFEST_NAME).read_text(encoding="utf-8"))
        return {name for name in data.get("files", []) if isinstance(name, str) and "/" not in name and "\\" not in name}
    except (OSError, ValueError, AttributeError):
        return set()


def write_asset_manifest(assets_dir: Path, assets: list[Asset]) -> None:
    if not assets:
        return
    payload = {"files": [asset.output for asset in assets]}
    (assets_dir / ASSET_MANIFEST_NAME).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def generated_asset_name(index: int, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"image-{index:03d}{ext}"


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
    total_bytes = 0
    for idx, stream in enumerate(bin_streams, start=1):
        source_name = stream[-1]
        raw_data = ole.openstream(stream).read()
        try:
            data = decode_hwp_bindata(raw_data)
            if total_bytes + len(data) > MAX_TOTAL_ASSET_BYTES:
                raise DecompressLimitError(f"문서 전체 그림 크기가 한도({format_megabytes(MAX_TOTAL_ASSET_BYTES)})를 넘습니다")
        except DecompressLimitError as exc:
            warnings.append(WarningItem("ASSET_SKIPPED", f"{ole_stream_name(stream)}: {exc}", ole_stream_name(stream)))
            continue
        total_bytes += len(data)
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
        out_name = generated_asset_name(idx, ext)
        out_path = assets_dir / out_name
        out_path.write_bytes(data)
        width, height = image_dimensions_from_bytes(data)
        assets.append(Asset(source=ole_stream_name(stream), output=out_name, kind="image", bytes=len(data), index=parse_bindata_index(source_name), width=width, height=height))
    write_asset_manifest(assets_dir, assets)
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
            decoded = bounded_zlib_decompress(data, wbits, MAX_ASSET_BYTES)
        except DecompressLimitError:
            raise
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

    # 행 단위 슬라이싱으로 BGR(A) -> RGB 변환한다(픽셀 단위 파이썬 루프는 대형 BMP에서 매우 느리다).
    bytes_per_pixel = bpp // 8
    raw = bytearray()
    for out_row in range(abs_height):
        src_row = abs_height - 1 - out_row if height > 0 else out_row
        row_start = pixel_offset + src_row * row_stride
        row = data[row_start : row_start + width * bytes_per_pixel]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::bytes_per_pixel]
        rgb[1::3] = row[1::bytes_per_pixel]
        rgb[2::3] = row[0::bytes_per_pixel]
        raw.append(0)  # PNG filter type: None
        raw.extend(rgb)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, abs_height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def append_image_anchor_if_caption(blocks: list[Block]) -> bool:
    # 마지막 본문 블록만 필요하므로 뒤에서부터 찾는다(전체 목록을 복사하면 도형이 많은 문서에서 제곱 시간이 걸린다).
    last = next((block for block in reversed(blocks) if block.type not in NON_CONTENT_BLOCK_TYPES), None)
    if last is None:
        return False
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
    script = hwp_equation_script(payload)
    if script is None:
        # 글자 수가 깨졌으면 머리(6바이트) 뒤부터 첫 NUL(글자 크기 필드) 앞까지를 스크립트로 보고 끝의 잡문자를 걷어낸다.
        script = payload[6:].decode("utf-16le", errors="ignore").split("\x00", 1)[0]
        script = re.split(r"HYhwpEQ|HancomEQN|Equation Version", script, maxsplit=1)[0]
        script = re.sub(r"[\x00-\x1f]+", " ", script)
        script = re.sub(r"[^0-9A-Za-z가-힣+\-*/=().,{}\[\]^_ %`~#&<>!|]+$", "", script).strip()
    if not script.strip():
        return None
    return hwp_equation_to_latex(script)


def hwp_equation_script(payload: bytes) -> Optional[str]:
    # EQEDIT 레코드: 속성(UINT32), 스크립트 글자 수(WORD), 스크립트(WCHAR), 이어서 글자 크기·색·버전·글꼴 이름.
    if len(payload) < 6:
        return None
    length = struct.unpack_from("<H", payload, 4)[0]
    end = 6 + length * 2
    if length == 0 or end > len(payload):
        return None
    return payload[6:end].decode("utf-16le", errors="ignore")


HWP_EQ_TOKEN_RE = re.compile(
    r'"[^"]*"'
    r"|\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
    r"|[A-Za-z]+"
    r"|[가-힣ㄱ-ㅎㅏ-ㅣ]+"
    r"|<->|<=>|->|<-|=>|<=|>=|!=|\+-|-\+|<<|>>"
    r"|\s+"
    r"|.",
    re.S,
)
HWP_EQ_GREEK = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu",
    "nu", "xi", "omicron", "pi", "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
]
# LaTeX에 대문자 명령이 없는 그리스 대문자는 모양이 같은 로마자로 쓴다.
HWP_EQ_GREEK_UPPER_LATIN = {
    "alpha": "A", "beta": "B", "epsilon": "E", "zeta": "Z", "eta": "H", "iota": "I", "kappa": "K",
    "mu": "M", "nu": "N", "omicron": "O", "rho": "P", "tau": "T", "chi": "X",
}
# 한글 수식에서 대소문자로 뜻이 갈리는 화살표(대문자는 겹화살표).
HWP_EQ_CASE_SENSITIVE = {
    "RARROW": r"\Rightarrow", "LARROW": r"\Leftarrow", "LRARROW": r"\Leftrightarrow",
    "UPARROW": r"\Uparrow", "DOWNARROW": r"\Downarrow",
}
HWP_EQ_SYMBOLS = {
    "times": r"\times", "div": r"\div", "cdot": r"\cdot", "cdots": r"\cdots", "ldots": r"\ldots",
    "vdots": r"\vdots", "ddots": r"\ddots", "inf": r"\infty", "infinity": r"\infty", "pm": r"\pm", "mp": r"\mp",
    "neq": r"\neq", "leq": r"\leq", "geq": r"\geq", "approx": r"\approx", "sim": r"\sim", "simeq": r"\simeq",
    "equiv": r"\equiv", "cong": r"\cong", "propto": r"\propto", "therefore": r"\therefore", "because": r"\because",
    "in": r"\in", "notin": r"\notin", "ni": r"\ni", "subset": r"\subset", "supset": r"\supset",
    "subseteq": r"\subseteq", "supseteq": r"\supseteq", "cup": r"\cup", "cap": r"\cap", "emptyset": r"\emptyset",
    "forall": r"\forall", "exist": r"\exists", "exists": r"\exists", "partial": r"\partial", "nabla": r"\nabla",
    "angle": r"\angle", "triangle": r"\triangle", "perp": r"\perp", "bot": r"\perp", "prime": "'",
    "deg": r"^{\circ}", "circ": r"\circ", "bullet": r"\bullet", "star": r"\star", "oplus": r"\oplus",
    "ominus": r"\ominus", "otimes": r"\otimes", "odot": r"\odot", "vee": r"\vee", "wedge": r"\wedge",
    "neg": r"\neg", "rarrow": r"\rightarrow", "larrow": r"\leftarrow", "lrarrow": r"\leftrightarrow",
    "uparrow": r"\uparrow", "downarrow": r"\downarrow", "mapsto": r"\mapsto", "hbar": r"\hbar", "ell": r"\ell",
    "aleph": r"\aleph", "dagger": r"\dagger", "parallel": r"\parallel", "lceil": r"\lceil", "rceil": r"\rceil",
    "lfloor": r"\lfloor", "rfloor": r"\rfloor", "vartheta": r"\vartheta", "varphi": r"\varphi",
    "varepsilon": r"\varepsilon", "varpi": r"\varpi", "varrho": r"\varrho", "varsigma": r"\varsigma",
    "sum": r"\sum", "prod": r"\prod", "coprod": r"\coprod", "int": r"\int", "dint": r"\iint", "tint": r"\iiint",
    "oint": r"\oint", "odint": r"\oiint", "otint": r"\oiiint", "inter": r"\bigcap", "union": r"\bigcup",
    "bigcap": r"\bigcap", "bigcup": r"\bigcup", "bigoplus": r"\bigoplus", "bigotimes": r"\bigotimes",
    "bigodot": r"\bigodot", "bigvee": r"\bigvee", "bigwedge": r"\bigwedge", "biguplus": r"\biguplus",
    "bigsqcup": r"\bigsqcup",
    "sin": r"\sin", "cos": r"\cos", "tan": r"\tan", "cot": r"\cot", "sec": r"\sec", "csc": r"\csc",
    "arcsin": r"\arcsin", "arccos": r"\arccos", "arctan": r"\arctan", "sinh": r"\sinh", "cosh": r"\cosh",
    "tanh": r"\tanh", "coth": r"\coth", "log": r"\log", "ln": r"\ln", "lg": r"\lg", "exp": r"\exp",
    "det": r"\det", "gcd": r"\gcd", "max": r"\max", "min": r"\min", "lim": r"\lim", "mod": r"\bmod",
}
HWP_EQ_OPERATORS = {
    "+-": r"\pm", "-+": r"\mp", "!=": r"\neq", "<=": r"\leq", ">=": r"\geq", "->": r"\rightarrow",
    "<-": r"\leftarrow", "<->": r"\leftrightarrow", "=>": r"\Rightarrow", "<=>": r"\Leftrightarrow",
    "<<": r"\ll", ">>": r"\gg", "%": r"\%", "$": r"\$", "\\": r"\backslash", "~": r"\ ", "`": r"\,",
}
HWP_EQ_ACCENTS = {
    "sqrt": r"\sqrt", "vec": r"\vec", "hat": r"\hat", "bar": r"\overline", "dot": r"\dot", "ddot": r"\ddot",
    "tilde": r"\tilde", "check": r"\check", "acute": r"\acute", "grave": r"\grave", "under": r"\underline",
    "dyad": r"\overleftrightarrow", "box": r"\boxed", "arch": r"\overset{\frown}",
}
HWP_EQ_ENVIRONMENTS = {
    "matrix": "matrix", "pmatrix": "pmatrix", "bmatrix": "bmatrix", "dmatrix": "vmatrix", "cases": "cases",
    "pile": "matrix", "lpile": "matrix", "rpile": "matrix", "eqalign": "aligned",
}
HWP_EQ_BRACKETS = {"{": r"\{", "}": r"\}", "<": r"\langle", ">": r"\rangle", "dline": r"\|", "line": "|"}
HWP_EQ_INFIX = {"over", "atop", "choose"}
HWP_EQ_SCRIPTS = {"^": "^", "_": "_", "sup": "^", "sub": "_", "from": "_", "to": "^", "lsup": "^", "lsub": "_"}
# 글꼴 모양만 바꾸는 명령은 LaTeX 본문에 영향을 주지 않도록 건너뛴다.
HWP_EQ_IGNORED = {"rm", "it", "bold"}


def latex_unbrace(text: str) -> str:
    """바깥을 감싼 { }가 하나의 짝이면 벗겨낸다."""
    if not (text.startswith("{") and text.endswith("}")):
        return text
    depth = 0
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and idx != len(text) - 1:
                return text
        idx += 1
    return text[1:-1]


class HwpEquationParser:
    """한글 수식 스크립트(over, sqrt, LEFT/RIGHT, matrix 등)를 LaTeX로 옮긴다."""

    def __init__(self, script: str) -> None:
        self.tokens = [tok for tok in HWP_EQ_TOKEN_RE.findall(script) if not tok.isspace()]
        self.pos = 0
        self.env_depth = 0
        self.top_level_rows = False

    def peek(self) -> str:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ""

    def parse(self) -> str:
        latex = self.parse_sequence(closing=False)
        if self.top_level_rows:
            latex = f"\\begin{{gathered}} {latex} \\end{{gathered}}"
        # 짝이 맞지 않는 LEFT/RIGHT는 LaTeX 오류가 나므로 크기 조절 없이 괄호만 남긴다.
        if len(re.findall(r"\\left(?![a-zA-Z])", latex)) != len(re.findall(r"\\right(?![a-zA-Z])", latex)):
            latex = re.sub(r"\\(?:left|right)(?![a-zA-Z])\.?", "", latex)
        return latex

    def parse_sequence(self, closing: bool) -> str:
        items: list[str] = []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            lower = tok.lower()
            if tok == "}":
                self.pos += 1
                if closing:
                    break
                continue
            if lower in HWP_EQ_INFIX:
                self.pos += 1
                left = latex_unbrace(items.pop()) if items else ""
                right = latex_unbrace(self.parse_operand())
                if lower == "over":
                    items.append(f"\\frac{{{left}}}{{{right}}}")
                elif lower == "choose":
                    items.append(f"\\binom{{{left}}}{{{right}}}")
                else:
                    items.append(f"{{{left} \\atop {right}}}")
                continue
            if tok in HWP_EQ_SCRIPTS or lower in HWP_EQ_SCRIPTS:
                self.pos += 1
                base = items.pop() if items else "{}"
                items.append(self.attach_script(base, HWP_EQ_SCRIPTS[lower]))
                continue
            items.append(self.parse_atom())
        return " ".join(item for item in items if item)

    def attach_script(self, base: str, mark: str) -> str:
        return f"{base}{mark}{{{latex_unbrace(self.parse_atom())}}}"

    def parse_operand(self) -> str:
        operand = self.parse_atom()
        while self.peek() in HWP_EQ_SCRIPTS or self.peek().lower() in HWP_EQ_SCRIPTS:
            mark = HWP_EQ_SCRIPTS[self.peek().lower()]
            self.pos += 1
            operand = self.attach_script(operand, mark)
        return operand

    def parse_group(self) -> str:
        if self.peek() == "{":
            self.pos += 1
            return "{" + self.parse_sequence(closing=True) + "}"
        return self.parse_atom()

    def parse_atom(self) -> str:
        if self.pos >= len(self.tokens):
            return ""
        tok = self.tokens[self.pos]
        self.pos += 1
        if tok == "{":
            return "{" + self.parse_sequence(closing=True) + "}"
        if tok == "#":
            if self.env_depth == 0:
                self.top_level_rows = True
            return r"\\"
        if tok == "&":
            return "&" if self.env_depth else ""
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            return latex_text(tok[1:-1])
        if re.match(r"[가-힣ㄱ-ㅎㅏ-ㅣ]", tok):
            return latex_text(tok)
        if tok[0].isdigit() or (tok[0] == "." and len(tok) > 1):
            return tok.replace(",", "{,}")
        if tok[0].isascii() and tok[0].isalpha():
            return self.parse_word(tok)
        return HWP_EQ_OPERATORS.get(tok, "\\" + tok if tok in "{}#&_^" else tok)

    def parse_word(self, word: str) -> str:
        lower = word.lower()
        if word in HWP_EQ_CASE_SENSITIVE:
            return HWP_EQ_CASE_SENSITIVE[word]
        if lower in HWP_EQ_GREEK:
            if word.islower():
                return "o" if lower == "omicron" else "\\" + lower
            return HWP_EQ_GREEK_UPPER_LATIN.get(lower, "\\" + lower.capitalize())
        if lower in HWP_EQ_IGNORED:
            return ""
        if lower in {"left", "right"}:
            bracket = self.tokens[self.pos] if self.pos < len(self.tokens) else "."
            self.pos += 1
            return f"\\{lower}{HWP_EQ_BRACKETS.get(bracket.lower(), bracket)}"
        if lower == "root":
            degree = latex_unbrace(self.parse_atom())
            if self.peek().lower() == "of":
                self.pos += 1
            return f"\\sqrt[{degree}]{{{latex_unbrace(self.parse_atom())}}}"
        if lower in HWP_EQ_ACCENTS:
            return f"{HWP_EQ_ACCENTS[lower]}{{{latex_unbrace(self.parse_atom())}}}"
        if lower in HWP_EQ_ENVIRONMENTS and self.peek() == "{":
            env = HWP_EQ_ENVIRONMENTS[lower]
            self.pos += 1
            self.env_depth += 1
            body = self.parse_sequence(closing=True)
            self.env_depth -= 1
            return f"\\begin{{{env}}} {body} \\end{{{env}}}"
        return HWP_EQ_SYMBOLS.get(lower, word)


def latex_text(text: str) -> str:
    text = re.sub(r"[{}\\]", "", text)
    text = re.sub(r"([%$#&_^~])", r"\\\1", text)
    return f"\\text{{{text}}}" if text else ""


def hwp_equation_to_latex(script: str) -> str:
    script = script.strip()
    try:
        latex = HwpEquationParser(script).parse()
    except RecursionError:
        return script
    return re.sub(r"\s+", " ", latex).strip()


def extract_hwp_footnote(records: list[tuple[int, int, bytes]], start_idx: int) -> tuple[str, int]:
    """각주/미주 컨트롤 아래에 중첩된 문단 텍스트를 모아 (텍스트, 다음 레코드 인덱스)를 돌려준다."""
    _tag, ctrl_level, _payload = records[start_idx]
    parts: list[str] = []
    idx = start_idx + 1
    while idx < len(records) and records[idx][1] > ctrl_level:
        if records[idx][0] == HWP_PARA_TEXT_TAG:
            text = clean_hwp_text_payload(records[idx][2])
            if text:
                parts.append(text)
        idx += 1
    return " ".join(parts).strip(), idx


def skip_nested_records(records: list[tuple[int, int, bytes]], start_idx: int) -> int:
    """컨트롤 레코드 아래에 중첩된 모든 레코드를 건너뛴 다음 인덱스를 돌려준다."""
    ctrl_level = records[start_idx][1]
    idx = start_idx + 1
    while idx < len(records) and records[idx][1] > ctrl_level:
        idx += 1
    return idx


def is_footnote_placeholder(block: Block) -> bool:
    return block.type == "footnote" and (block.text or "").startswith("HWP 각주 내용은")


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
        elif block.type != "equation" and block.text:
            # 칸 하나짜리 표는 문단으로 풀리므로 그 안의 수식도 센다.
            total += block.text.count(r"\(")
    return total


def count_footnote_fallbacks(blocks: list[Block]) -> int:
    return sum(1 for block in blocks if is_footnote_placeholder(block))


def count_cell_footnotes(blocks: list[Block]) -> int:
    """표 칸 안에 "(각주: ...)"로 복원된 각주 수."""
    total = 0
    for block in content_blocks(blocks):
        if block.type != "table":
            # 표가 문단으로 풀린 경우에도 칸의 각주는 문단 텍스트 안에 들어 있다.
            total += (block.text or "").count(HWP_CELL_FOOTNOTE_OPEN)
            continue
        if block.cells:
            total += sum(cell.text.count(HWP_CELL_FOOTNOTE_OPEN) for cell in block.cells)
        elif block.rows:
            total += sum(cell.count(HWP_CELL_FOOTNOTE_OPEN) for row in block.rows for cell in row)
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


def add_page_artifact_warnings(blocks: list[Block], warnings: list[WarningItem], losses: list[LossItem]) -> None:
    dropped = [block.text or "" for block in blocks if block.type == "page_artifact"]
    if not dropped:
        return
    preview = " / ".join(text[:60] for text in dropped[:3])
    warnings.append(WarningItem(
        "PAGE_ARTIFACT_TABLE_DROPPED",
        f"결재란/머리말/꼬리말로 판단한 표 {len(dropped)}개를 출력에서 제외했습니다. 포함하려면 --include-page-artifacts를 사용하세요. 예: {preview}",
    ))
    losses.append(LossItem("PAGE_ARTIFACT_TABLE_DROPPED", len(dropped)))


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
        # 배포용 문서는 BodyText에 "배포용 문서입니다" 안내문만 있고 실제 본문은 ViewText에 암호화되어 있다.
        distributed = bool(flags & 0x04)
        body_storage = "ViewText" if distributed else "BodyText"
        if distributed:
            warnings.append(WarningItem("DISTRIBUTION_DOC_DECRYPTED", "배포용 문서(복사·인쇄 제한)의 본문을 풀어 변환했습니다. 작성자가 정한 이용 범위 안에서 사용하세요."))
        elif flags & 0x4000:
            # 변경 추적 문서는 BodyText에 최종본을, ViewText에 삭제 표시까지 담은 수정 표시본을 둔다.
            warnings.append(WarningItem("TRACK_CHANGES_FINAL_TEXT", "변경 추적 문서입니다. 수정 내용을 반영한 최종본만 변환했고 삭제 표시된 글자는 넣지 않았습니다."))

        sections = [s for s in ole.listdir(streams=True, storages=False) if len(s) == 2 and s[0] == body_storage and s[1].lower().startswith("section")]
        sections.sort(key=lambda s: int(re.sub(r"\D+", "", s[1]) or "0"))
        if not sections:
            raise ConversionError(f"{body_storage}/Section 스트림을 찾을 수 없습니다.")

        para_count = 0
        record_count = 0
        control_counts: dict[bytes, int] = {}
        seen_header_footer: set[str] = set()
        empty_footnotes = 0
        repeated_header_footer = 0
        for section in sections:
            try:
                data = read_hwp_stream(ole, section, compressed, distributed)
            except Exception as exc:
                warnings.append(WarningItem("SECTION_DECOMPRESS_FAILED", f"{ole_stream_name(section)} {'복호화/' if distributed else ''}압축 해제 실패: {exc}", ole_stream_name(section)))
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
                        document_has_blocks=has_content_blocks(blocks),
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
                        footnote_text, next_idx = extract_hwp_footnote(records, idx)
                        if footnote_text:
                            blocks.append(Block(type="footnote", text=footnote_text))
                        else:
                            empty_footnotes += 1
                        record_count += max(0, next_idx - idx - 1)
                        idx = next_idx
                        continue
                    elif control_id in HWP_HEADER_FOOTER_CTRL_IDS:
                        # 머리말/꼬리말은 구역마다 같은 내용이 반복되므로 처음 한 번만 본문에 남기고 반복은 제외한다.
                        # 내용이 본문 정보일 수도 있어 통째로 버리지는 않는다.
                        next_idx = skip_nested_records(records, idx)
                        for hf_tag, _hf_level, hf_payload in records[idx + 1 : next_idx]:
                            if hf_tag != HWP_PARA_TEXT_TAG:
                                continue
                            hf_text = clean_hwp_text_payload(hf_payload)
                            if not hf_text:
                                continue
                            if hf_text in seen_header_footer:
                                repeated_header_footer += 1
                                continue
                            seen_header_footer.add(hf_text)
                            for part in split_text_to_candidate_blocks(hf_text):
                                append_text_block(blocks, part, options.heading_policy, is_first=not has_content_blocks(blocks), include_page_artifacts=options.include_page_artifacts)
                        record_count += max(0, next_idx - idx - 1)
                        idx = next_idx
                        continue
                if tag_id == HWP_PARA_TEXT_TAG:
                    para_count += 1
                    text = clean_hwp_text_payload(payload, hwp_paragraph_controls(records, idx))
                    for part in split_text_to_candidate_blocks(text):
                        append_text_block(blocks, part, options.heading_policy, is_first=not has_content_blocks(blocks), include_page_artifacts=options.include_page_artifacts)
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
                    # PrvText는 표 칸을 "<칸 내용>"으로 감싼다. 빈 칸 "<>"가 제목/문단으로 나오지 않게 지운다.
                    preview = re.sub(r"<\s*>", "", preview)
                    for part in split_text_to_candidate_blocks(clean_plain_text(preview)):
                        append_text_block(blocks, part, options.heading_policy, include_page_artifacts=options.include_page_artifacts)
                    warnings.append(WarningItem("PREVIEW_TEXT_USED", "BodyText 추출 결과가 비어 있어 PrvText를 fallback으로 사용했습니다."))
                except Exception:
                    pass

        losses.append(LossItem("LAYOUT_DROPPED", 1))
        losses.append(LossItem("CHARACTER_STYLE_PARTIALLY_DROPPED", 1))
        add_page_artifact_warnings(blocks, warnings, losses)
        if repeated_header_footer:
            warnings.append(WarningItem(
                "HEADER_FOOTER_REPEATS_COLLAPSED",
                f"구역마다 반복되는 머리말/꼬리말 {repeated_header_footer}개는 처음 한 번만 남기고 제외했습니다.",
            ))
        if empty_footnotes:
            warnings.append(WarningItem("EMPTY_FOOTNOTES_SKIPPED", f"내용이 비어 있는 각주/미주 {empty_footnotes}개는 출력하지 않았습니다."))
        add_asset_position_warnings(assets, blocks, assets_dir, warnings, losses)
        footnote_fallback = count_footnote_fallbacks(blocks)
        add_hwp_control_warnings(
            control_counts,
            warnings,
            losses,
            equation_fallback_count=count_equation_fallbacks(blocks),
            footnote_fallback_count=footnote_fallback,
            footnote_restored_count=max(0, count_blocks(blocks, "footnote") - sum(1 for b in blocks if is_footnote_placeholder(b))) + empty_footnotes + count_cell_footnotes(blocks),
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
    # 숫자만 있는 문단은 목차 번호·제목 번호·단 번호 같은 본문일 수 있어 지우지 않는다.
    # 쪽 번호는 꼬리말 컨트롤로 따로 처리된다.
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
    list_match = re.match(r"^(?P<marker>(?:[-*+])|(?:[•·∙◦ㆍ□○〇■▶▷◇◆※ㅇ●❍❑❖▪➔⇨➢✓])|(?:(?:\(\d{1,3}\)|\d{1,3}[.)])(?!\d))|(?:[가-하]\.)|(?:[A-Za-z][.)]))\s*(?P<body>.+)$", text)
    if list_match:
        marker = list_match.group("marker")
        body = list_match.group("body").strip()
        if re.match(r"^\(?\d+\)?[.)]$", marker):
            blocks.append(Block(type="ordered_list", text=body, number=int(re.sub(r"\D", "", marker))))
            return
        # 가./나./A. 같은 항목 기호는 본문 구조를 나타내므로 글머리표로 바꾸되 기호는 보존한다.
        if re.match(r"^(?:[가-하]\.|[A-Za-z][.)])$", marker):
            body = f"{marker} {body}"
        blocks.append(Block(type="bullet_list", text=body))
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
    if is_first and len(stripped) <= 80 and not stripped.isdigit():
        return 1
    if re.match(r"^(제\s*\d+\s*[장절관항]|제\s*[一二三四五六七八九十]+\s*[장절관항])\b", stripped) and len(stripped) <= 60:
        return 2
    if re.match(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.)]\s+", stripped) and len(stripped) <= 60:
        return 2
    # 번호가 붙어도 문장으로 끝나는 조항·목록 항목("…하여야 한다.", "…일 것", "…에 따른다:")과
    # 연도로 시작하는 날짜("2016. 8. 12. 시행된 …")는 제목이 아니다.
    numbered_body = re.search(r"(?:다|것)\.?$|:$", stripped) or re.match(r"^(?:1[89]|20)\d{2}\.\s*\d{1,2}\.", stripped)
    if re.match(r"^\d+\.\s+\S", stripped) and len(stripped) <= 80 and not numbered_body:
        return 2
    if re.match(r"^\d+(?:\.\d+)+\.?\s+\S", stripped) and len(stripped) <= 90 and not numbered_body:
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
        if is_encrypted_hwpx(zf, names):
            raise ConversionError("암호화된 HWPX 문서는 지원하지 않습니다. 한글에서 암호를 해제한 뒤 다시 변환하세요.")
        section_names = sorted([n for n in names if re.search(r"(?:^|/)section\d+\.xml$", n, re.I)])
        if not section_names:
            # 일부 HWPX는 Contents/content.hpf에서 section 목록을 간접 참조한다.
            section_names = sorted([n for n in names if n.lower().endswith(".xml") and "section" in n.lower()])
        if not section_names:
            raise ConversionError("HWPX section XML을 찾을 수 없습니다.")

        for section_name in section_names:
            try:
                section_data = read_zip_member(zf, zf.getinfo(section_name), MAX_SECTION_BYTES)
            except ZipMemberError as exc:
                warnings.append(WarningItem("HWPX_ENTRY_SKIPPED", f"{section_name}: {exc}", section_name))
                continue
            try:
                root = ET.fromstring(section_data)
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
            total_bytes = 0
            for idx, name in enumerate(bin_names, start=1):
                info = zf.getinfo(name)
                try:
                    if total_bytes + info.file_size > MAX_TOTAL_ASSET_BYTES:
                        raise ZipMemberError(f"문서 전체 그림 크기가 한도({format_megabytes(MAX_TOTAL_ASSET_BYTES)})를 넘습니다")
                    data = read_zip_member(zf, info, MAX_ASSET_BYTES)
                except ZipMemberError as exc:
                    warnings.append(WarningItem("ASSET_SKIPPED", f"{name}: {exc}", name))
                    continue
                total_bytes += len(data)
                ext = Path(name).suffix.lower() or guess_extension(data)
                if ext == ".bmp":
                    png_data = bmp_to_png(data)
                    if png_data:
                        data = png_data
                        ext = ".png"
                out_name = generated_asset_name(idx, ext)
                (assets_dir / out_name).write_bytes(data)
                width, height = image_dimensions_from_bytes(data)
                assets.append(Asset(source=name, output=out_name, kind="image", bytes=len(data), index=parse_bindata_index(name), width=width, height=height))
            write_asset_manifest(assets_dir, assets)
            if assets:
                warnings.append(WarningItem("ASSET_POSITION_APPROXIMATED", "HWPX 이미지의 본문 내 배치 위치를 완전히 해석하지 못한 항목은 첨부 목록으로 보존합니다."))

    if not blocks:
        warnings.append(WarningItem("NO_TEXT_BLOCKS", "추출된 텍스트 블록이 없습니다."))
    losses.append(LossItem("LAYOUT_DROPPED", 1))
    losses.append(LossItem("CHARACTER_STYLE_PARTIALLY_DROPPED", 1))
    report = build_report(input_path, "hwpx", blocks, assets, warnings, losses)
    markdown = render_markdown(input_path, blocks, assets, report, options, output_path, assets_dir)
    return ConvertResult(markdown=markdown, report=report, blocks=blocks, assets=assets)


HWPML_TAG_MAP = {
    "P": "p", "TABLE": "tbl", "ROW": "tr", "CELL": "tc", "CAPTION": "caption", "LINEBREAK": "lineBreak",
    "TAB": "tab", "EQUATION": "equation", "SCRIPT": "script",
}


def hwpml_to_hwpx_tree(elem: ET.Element, open_fields: Optional[list[str]] = None) -> ET.Element:
    """HWPML(한글 XML) 본문을 HWPX와 같은 이름의 트리로 옮겨 HWPX 변환 경로를 그대로 쓰게 한다."""
    if open_fields is None:
        open_fields = []
    if elem.tag == "FIELDBEGIN":
        # HWPML 필드 끝에는 짝 정보가 없어 문서 순서대로 쌓아 두었다가 이어 준다.
        field_id = elem.get("InstId") or str(len(open_fields))
        open_fields.append(field_id)
        node = ET.Element("fieldBegin", {"type": (elem.get("Type") or "").upper(), "id": field_id})
        if elem.get("Command"):
            ET.SubElement(node, "stringParam", {"name": "Command"}).text = elem.get("Command")
        return node
    if elem.tag == "FIELDEND":
        return ET.Element("fieldEnd", {"beginIDRef": open_fields.pop() if open_fields else ""})
    if elem.tag == "AUTONUM":
        node = ET.Element("autoNum", {"num": elem.get("Number", ""), "numType": (elem.get("NumberType") or "").upper()})
        fmt = elem.find("AUTONUMFORMAT")
        if fmt is not None:
            ET.SubElement(node, "autoNumFormat", {
                "type": fmt.get("Type", ""), "prefixChar": fmt.get("PrefixChar", ""), "suffixChar": fmt.get("SuffixChar", ""),
            })
        return node
    node = ET.Element(HWPML_TAG_MAP.get(elem.tag, elem.tag), dict(elem.attrib))
    # 문단 밖 SECTION 등에 들어 있는 서식 자리표시자({머리글} 등)는 본문이 아니므로 CHAR·SCRIPT 글자만 옮긴다.
    if elem.tag in {"CHAR", "SCRIPT"}:
        node.text = elem.text
    for child in elem:
        node.append(hwpml_to_hwpx_tree(child, open_fields))
        if elem.tag == "CHAR":
            node[-1].tail = child.tail
    if elem.tag == "CELL":
        ET.SubElement(node, "cellAddr", {"colAddr": elem.get("ColAddr", "0"), "rowAddr": elem.get("RowAddr", "0")})
        ET.SubElement(node, "cellSpan", {"colSpan": elem.get("ColSpan", "1"), "rowSpan": elem.get("RowSpan", "1")})
    return node


def convert_hwpml(input_path: Path, output_path: Path, assets_dir: Path, options: ConvertOptions) -> ConvertResult:
    warnings: list[WarningItem] = []
    losses: list[LossItem] = []
    blocks: list[Block] = []
    assets: list[Asset] = []

    if input_path.stat().st_size > MAX_SECTION_BYTES + MAX_TOTAL_ASSET_BYTES:
        raise ConversionError(f"HWPML 파일이 너무 큽니다 (한도 {format_megabytes(MAX_SECTION_BYTES + MAX_TOTAL_ASSET_BYTES)}).")
    try:
        root = ET.parse(input_path).getroot()
    except ET.ParseError as exc:
        raise ConversionError(f"HWPML XML을 읽을 수 없습니다 ({exc}).") from exc
    body = root.find("BODY")
    if body is None:
        raise ConversionError("HWPML BODY 요소를 찾을 수 없습니다.")
    walk_hwpx_blocks(hwpml_to_hwpx_tree(body), blocks, options.heading_policy, options.include_page_artifacts)

    if options.image_mode != "skip":
        # 그림은 TAIL/BINDATASTORAGE에 base64로 들어 있다. 적힌 Format이 실제와 다를 수 있어 내용으로 판별한다.
        formats = {item.get("BinData"): item.get("Format", "") for item in root.iter("BINITEM")}
        bin_items = [b for b in root.iter("BINDATA") if (b.text or "").strip()]
        if bin_items:
            assets_dir.mkdir(parents=True, exist_ok=True)
            if options.clean_assets:
                removed = clean_generated_assets(assets_dir, safe_stem(input_path))
                if removed:
                    warnings.append(WarningItem("ASSETS_CLEANED", f"이전 변환에서 생성된 자산 {removed}개를 정리했습니다."))
        total_bytes = 0
        for idx, item in enumerate(bin_items, start=1):
            source = f"BINDATA/{item.get('Id', idx)}"
            try:
                data = base64.b64decode(item.text or "", validate=False)
            except (ValueError, binascii.Error) as exc:
                warnings.append(WarningItem("ASSET_SKIPPED", f"{source}: base64 해석 실패 ({exc})", source))
                continue
            if guess_extension(data) == ".bin":
                try:
                    data = bounded_zlib_decompress(data, -15, MAX_ASSET_BYTES)
                except (zlib.error, DecompressLimitError):
                    pass
            if len(data) > MAX_ASSET_BYTES or total_bytes + len(data) > MAX_TOTAL_ASSET_BYTES:
                warnings.append(WarningItem("ASSET_SKIPPED", f"{source}: 그림 크기가 한도를 넘습니다", source))
                continue
            total_bytes += len(data)
            ext = guess_extension(data)
            if ext == ".bin" and formats.get(item.get("Id")):
                ext = "." + formats[item.get("Id")].lower()
            if ext == ".bmp":
                png_data = bmp_to_png(data)
                if png_data:
                    data = png_data
                    ext = ".png"
            out_name = generated_asset_name(idx, ext)
            (assets_dir / out_name).write_bytes(data)
            width, height = image_dimensions_from_bytes(data)
            index = int(item.get("Id")) if (item.get("Id") or "").isdigit() else None
            assets.append(Asset(source=source, output=out_name, kind="image", bytes=len(data), index=index, width=width, height=height))
        if bin_items:
            write_asset_manifest(assets_dir, assets)
        if assets:
            warnings.append(WarningItem("ASSET_POSITION_APPROXIMATED", "HWPML 이미지의 본문 내 배치 위치를 해석하지 못해 첨부 목록으로 보존합니다."))

    if not blocks:
        warnings.append(WarningItem("NO_TEXT_BLOCKS", "추출된 텍스트 블록이 없습니다."))
    losses.append(LossItem("LAYOUT_DROPPED", 1))
    losses.append(LossItem("CHARACTER_STYLE_PARTIALLY_DROPPED", 1))
    report = build_report(input_path, "hwpml", blocks, assets, warnings, losses)
    markdown = render_markdown(input_path, blocks, assets, report, options, output_path, assets_dir)
    return ConvertResult(markdown=markdown, report=report, blocks=blocks, assets=assets)


def is_encrypted_hwpx(zf: zipfile.ZipFile, names: list[str]) -> bool:
    # 암호를 건 HWPX는 META-INF/manifest.xml의 항목마다 ODF 방식의 encryption-data를 적는다.
    manifest = next((n for n in names if n.lower() == "meta-inf/manifest.xml"), None)
    if manifest is None:
        return False
    try:
        data = read_zip_member(zf, zf.getinfo(manifest), MAX_SECTION_BYTES)
    except ZipMemberError:
        return False
    return b"encryption-data" in data


def local_name(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag


def collect_text(elem: ET.Element, skip: Optional[set[int]] = None) -> str:
    # HWPX도 HWP와 같이 Wingdings/Symbol 글꼴 기호를 사용자 정의 영역 글자로 저장한다.
    return finalize_link_marks(clean_plain_text(collect_text_with_inline_styles(elem, skip).translate(HWP_SYMBOL_FONT_MAP)))


def collect_text_with_inline_styles(elem: ET.Element, skip: Optional[set[int]] = None) -> str:
    name = local_name(elem)
    if name in {"lineBreak", "br"}:
        return "\n"
    if name == "tab":
        return "\t"
    if name == "equation":
        # 수식은 <hp:script>의 한글 수식 스크립트만 LaTeX로 옮기고 설명문(shapeComment) 등은 넣지 않는다.
        script = next((child.text for child in elem.iter() if local_name(child) == "script"), None) or ""
        return rf"\({hwp_equation_to_latex(script)}\)" if script.strip() else ""
    if name == "fieldBegin":
        # 필드 매개변수(명령 문자열, 메모 정보 등)는 본문이 아니다. 하이퍼링크만 링크 표식을 남긴다.
        if (elem.get("type") or "").upper() == "HYPERLINK":
            command = next((p.text or "" for p in elem.iter() if local_name(p) == "stringParam" and p.get("name") == "Command"), "")
            url = hyperlink_url(command)
            if url:
                return link_open_mark(re.sub(r"\D", "", elem.get("id") or "") or "0", url)
        return ""
    if name == "fieldEnd":
        return link_close_mark(re.sub(r"\D", "", elem.get("beginIDRef") or "") or "0")
    if name == "autoNum":
        if (elem.get("numType") or "").upper() not in CAPTION_NUMBER_TYPES or not (elem.get("num") or "").isdigit():
            return ""
        fmt = next((child for child in elem if local_name(child) == "autoNumFormat"), None)
        shape = AUTO_NUMBER_SHAPES.get(re.sub(r"[^A-Z]", "", (fmt.get("type") or "").upper()), 0) if fmt is not None else 0
        prefix = fmt.get("prefixChar", "") if fmt is not None else ""
        suffix = fmt.get("suffixChar", "") if fmt is not None else ""
        return f"{prefix}{format_auto_number(int(elem.get('num')), shape)}{suffix}"
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        if not (skip and id(child) in skip):
            parts.append(collect_text_with_inline_styles(child, skip))
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


HWPX_TABLE_NAMES = {"tbl", "table"}


def hwpx_top_level_tables(elem: ET.Element) -> list[ET.Element]:
    """elem 아래에서 다른 표 안에 들어 있지 않은 최상위 표만 찾는다."""
    found: list[ET.Element] = []
    for child in list(elem):
        if local_name(child) in HWPX_TABLE_NAMES:
            found.append(child)
        else:
            found.extend(hwpx_top_level_tables(child))
    return found


def hwpx_paragraph_lines(container: ET.Element) -> list[str]:
    """셀/캡션 안의 문단을 줄 단위로 모은다. 중첩된 표는 행 단위 줄로 풀어 쓴다."""
    lines: list[str] = []
    for child in list(container):
        name = local_name(child)
        if name == "p":
            nested = hwpx_top_level_tables(child)
            text = collect_text(child, {id(t) for t in nested})
            if text:
                lines.extend(part for part in text.split(chr(10)) if part.strip())
            for table in nested:
                for tr in [x for x in table.iter() if local_name(x) in {"tr", "row"}]:
                    row_texts = []
                    for tc in [x for x in list(tr) if local_name(x) in {"tc", "cell"}]:
                        row_texts.append(" ".join(hwpx_paragraph_lines(tc)))
                    if any(row_texts):
                        lines.append(" ".join(t for t in row_texts if t))
        elif name in HWPX_TABLE_NAMES:
            continue
        else:
            lines.extend(hwpx_paragraph_lines(child))
    return lines


def parse_hwpx_table(table: ET.Element) -> Optional[tuple[int, int, list[TableCell], list[str]]]:
    cells: list[TableCell] = []
    caption_lines: list[str] = []
    for child in list(table):
        if local_name(child) == "caption":
            caption_lines.extend(hwpx_paragraph_lines(child))
    for row_pos, tr in enumerate([x for x in list(table) if local_name(x) in {"tr", "row"}]):
        col_pos = 0
        for tc in [x for x in list(tr) if local_name(x) in {"tc", "cell"}]:
            addr = next((x for x in list(tc) if local_name(x) == "cellAddr"), None)
            span = next((x for x in list(tc) if local_name(x) == "cellSpan"), None)

            def number(node: Optional[ET.Element], key: str, default: int) -> int:
                try:
                    return max(0, int(node.get(key))) if node is not None and node.get(key) is not None else default
                except ValueError:
                    return default

            row = number(addr, "rowAddr", row_pos)
            col = number(addr, "colAddr", col_pos)
            rowspan = max(1, number(span, "rowSpan", 1))
            colspan = max(1, number(span, "colSpan", 1))
            text = "\n".join(hwpx_paragraph_lines(tc))
            cells.append(TableCell(row=row, col=col, text=text, rowspan=rowspan, colspan=colspan))
            col_pos = col + colspan
    if not cells:
        return None
    rows = max(cell.row + cell.rowspan for cell in cells)
    cols = max(cell.col + cell.colspan for cell in cells)
    return rows, cols, cells, caption_lines


def hwpx_table_blocks(table: ET.Element, blocks: list[Block], heading_policy: str, include_page_artifacts: bool) -> list[Block]:
    parsed = parse_hwpx_table(table)
    if not parsed:
        return []
    rows, cols, cells, caption_lines = parsed
    return finalize_table_blocks(rows, cols, cells, caption_lines, heading_policy, has_content_blocks(blocks), include_page_artifacts)


def walk_hwpx_blocks(elem: ET.Element, blocks: list[Block], heading_policy: str, include_page_artifacts: bool = False) -> None:
    name = local_name(elem)
    if name in HWPX_TABLE_NAMES:
        blocks.extend(hwpx_table_blocks(elem, blocks, heading_policy, include_page_artifacts))
        return
    if name == "p":
        # HWPX는 표가 문단(p > run > tbl) 안에 들어 있다. 표를 뺀 문단 텍스트를 먼저 내고 표는 따로 복원한다.
        nested_tables = hwpx_top_level_tables(elem)
        if nested_tables:
            text = collect_text(elem, {id(t) for t in nested_tables})
            for part in split_text_to_candidate_blocks(text):
                append_text_block(blocks, part, heading_policy, include_page_artifacts=include_page_artifacts)
            for table in nested_tables:
                blocks.extend(hwpx_table_blocks(table, blocks, heading_policy, include_page_artifacts))
            return
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
    return [block for block in blocks if block.type not in NON_CONTENT_BLOCK_TYPES]


def has_content_blocks(blocks: list[Block]) -> bool:
    """본문 블록이 하나라도 있는지 확인한다. 목록 전체를 복사하지 않고 첫 본문 블록에서 바로 끝낸다."""
    return any(block.type not in NON_CONTENT_BLOCK_TYPES for block in blocks)


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
            if block.number is not None:
                ordered_index = block.number
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
        # 추출할 때 이미 잰 크기를 쓴다. 그림이 수천 개인 문서에서 파일을 매번 다시 읽으면 수십 초가 걸린다.
        width, height = asset.width, asset.height
        if width is None and height is None:
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
    norm = [row for row in norm if any(cell.strip() for cell in row)]
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
    # 링크는 화면에 보이는 글자만 센다. 주소 길이 때문에 머리행이 본문으로 밀리지 않게 한다.
    non_empty = [re.sub(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\([^)\s]*\)", r"\1", cell).strip() for cell in row if cell.strip()]
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
            # 내용이 전혀 없고 다른 셀의 rowspan이 걸치지도 않은 빈 행은 출력하지 않는다.
            covered_by_span = any(cell.row < ridx < cell.row + cell.rowspan for cell in cells)
            if ridx > 0 and not covered_by_span and all(not cell.text.strip() and cell.rowspan == 1 for cell in row_cells):
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
    # 셀 텍스트에는 줄바꿈을 뜻하는 "<br>"가 이미 들어 있을 수 있다. <br> 단위로 나눠 각 조각만 이스케이프한다.
    # 구조 문자 이스케이프를 먼저 하고 파이프를 처리해야 "\|"가 "\\|"로 겹치지 않는다.
    # 조각 안의 "<...>"는 HTML 태그로 해석되지 않도록 엔티티로 바꾼다.
    parts = text.replace("\n", "<br>").split("<br>")
    escaped = [escape_md_text(part).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;") for part in parts]
    return "<br>".join(escaped)


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
    try:
        if fmt == "hwp":
            result = convert_hwp(input_path, output_path, assets_dir, options)
        elif fmt == "hwpx":
            result = convert_hwpx(input_path, output_path, assets_dir, options)
        elif fmt == "hwpml":
            result = convert_hwpml(input_path, output_path, assets_dir, options)
        else:
            raise ConversionError(f"지원하지 않는 파일 형식입니다: {fmt}")
    except ConversionError:
        raise
    except CORRUPT_FILE_ERRORS as exc:
        # 손상된 파일에서 olefile/zipfile/zlib 내부 오류가 그대로 보이지 않게 변환 오류로 바꾼다.
        # 코드 자체의 버그(TypeError 등)는 가리지 않도록 데이터 손상에서 나오는 예외만 잡는다.
        raise ConversionError(f"손상된 파일이거나 읽을 수 없는 구조입니다 ({type(exc).__name__}: {str(exc)[:120]})") from exc
    except OSError as exc:
        # olefile의 OleFileError/NotOleFileError는 OSError 하위 클래스다. 그 외 파일 입출력 오류는 그대로 둔다.
        if not type(exc).__module__.startswith("olefile"):
            raise
        raise ConversionError(f"손상된 파일이거나 읽을 수 없는 구조입니다 ({type(exc).__name__}: {str(exc)[:120]})") from exc

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
    parser.add_argument("input", nargs="?", help="입력 .hwp, .hwpx 또는 .hml(HWPML) 파일")
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
        if input_path.is_dir():
            raise ConversionError(f"폴더는 직접 변환할 수 없습니다. --batch \"{input_path}/*.hwp\" 형식을 사용하세요.")
        if not input_path.is_file():
            raise ConversionError(f"입력 파일을 찾을 수 없습니다: {input_path}")
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
