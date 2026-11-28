"""테스트용 최소 HWP 5.x 파일 생성기.

저장소에는 실제 `.hwp` 표본이 없다(`.gitignore`의 `*.hwp`). 그래서 OLE(Compound File
Binary) 컨테이너와 HWP 레코드 스트림을 테스트 안에서 직접 조립해, `parse_hwp_records`와
`convert_hwp`가 실제 OLE/HWP 경로를 그대로 타게 한다.

만들어 내는 파일은 한글에서 열리도록 만든 완전한 문서가 아니라, hwp2md가 읽는 구조
(FileHeader 서명·압축 플래그, BodyText/Section0 레코드, BinData 스트림)만 정확히 갖춘
최소 파일이다. 스트림은 모두 4096바이트 이상으로 채워 OLE mini stream(miniFAT)을 쓰지
않는다.
"""

from __future__ import annotations

import struct
import zlib

SECTOR_SIZE = 512
MINI_CUTOFF = 4096
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF

HWP_PARA_TEXT_TAG = 67
HWP_CTRL_HEADER_TAG = 71
HWP_TABLE_CELL_TAG = 72
HWP_TABLE_TAG = 77
HWP_PICTURE_RECORD_TAG = 85
HWP_TABLE_CTRL_ID = b" lbt"
HWP_SHAPE_CTRL_ID = b" osg"
HWP_PICTURE_BIN_OFFSET = 71


def record(tag_id: int, level: int, payload: bytes) -> bytes:
    """HWP 레코드 하나를 만든다. 크기가 0xFFF 이상이면 확장 크기 필드를 붙인다."""
    size = len(payload)
    if size < 0xFFF:
        return struct.pack("<I", tag_id | (level << 10) | (size << 20)) + payload
    return struct.pack("<I", tag_id | (level << 10) | (0xFFF << 20)) + struct.pack("<I", size) + payload


def paragraph(text: str, level: int = 0) -> bytes:
    """PARA_TEXT 레코드. HWP 본문 문단은 UTF-16LE 글자로 저장된다."""
    return record(HWP_PARA_TEXT_TAG, level, text.encode("utf-16le"))


def table(rows: int, cols: int, texts: list[str]) -> bytes:
    """표 컨트롤(CTRL_HEADER → TABLE → TABLE_CELL → 문단) 레코드를 만든다."""
    out = record(HWP_CTRL_HEADER_TAG, 0, HWP_TABLE_CTRL_ID + b"\x00" * 12)
    out += record(HWP_TABLE_TAG, 1, b"\x00" * 4 + struct.pack("<HH", rows, cols) + b"\x00" * 8)
    for row in range(rows):
        for col in range(cols):
            text = texts[row * cols + col] if row * cols + col < len(texts) else ""
            out += record(HWP_TABLE_CELL_TAG, 1, b"\x00" * 8 + struct.pack("<HHHH", col, row, 1, 1))
            if text:
                out += paragraph(text, level=2)
    return out


def picture(bin_id: int) -> bytes:
    """그림 개체(gso) 컨트롤. SHAPE_COMPONENT_PICTURE 레코드의 offset 71에 BinData ID가 있다."""
    payload = bytearray(max(73, HWP_PICTURE_BIN_OFFSET + 2))
    struct.pack_into("<H", payload, HWP_PICTURE_BIN_OFFSET, bin_id)
    return record(HWP_CTRL_HEADER_TAG, 0, HWP_SHAPE_CTRL_ID + b"\x00" * 12) + record(
        HWP_PICTURE_RECORD_TAG, 1, bytes(payload)
    )


def png(width: int = 800, height: int = 600) -> bytes:
    """크기 정보를 담은 최소 PNG(서명 + IHDR + IEND)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def file_header(compressed: bool = True) -> bytes:
    """HWP FileHeader 스트림(256바이트). 36번째 바이트부터 속성 플래그가 온다."""
    data = bytearray(256)
    data[0:32] = b"HWP Document File".ljust(32, b"\x00")
    struct.pack_into("<I", data, 32, 0x00050003)  # 버전 5.0.3.0
    struct.pack_into("<I", data, 36, 0x01 if compressed else 0x00)
    return bytes(data)


def section_stream(records: bytes, compressed: bool) -> bytes:
    """섹션 스트림. 실제 HWP는 문단 레코드를 raw deflate으로 압축해 넣는다.

    4096바이트보다 짧으면 OLE mini stream이 필요해지므로, 의미 없는 태그(0) 레코드로
    채워 크기를 늘린다(hwp2md는 태그 0을 해석하지 않고 건너뛴다).
    """
    body = zlib.compress(records)[2:-4] if compressed else records
    if len(body) + 4 >= MINI_CUTOFF:
        return body
    return body + record(0, 0, b"\x00" * (MINI_CUTOFF - len(body) - 4))


def _pad_stream(data: bytes) -> bytes:
    """OLE regular stream은 512바이트 배수를 요구하고, 4096 미만이면 mini stream에 들어간다."""
    size = max(MINI_CUTOFF, (len(data) + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE)
    return data + b"\x00" * (size - len(data))


class _Entry:
    __slots__ = ("name", "kind", "data", "children", "left", "right", "child", "sector", "size")

    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.kind = kind  # "root", "storage", "stream"
        self.data = b""
        self.children: list[_Entry] = []
        self.left = NOSTREAM
        self.right = NOSTREAM
        self.child = NOSTREAM
        self.sector = ENDOFCHAIN
        self.size = 0


def build_cfb(streams: list[tuple[list[str], bytes]]) -> bytes:
    """(경로, 데이터) 목록으로 최소 OLE 복합 문서를 만든다.

    헤더 1섹터 + FAT 섹터 + 디렉터리 섹터 + 스트림 섹터 순으로 배치한다.
    모든 노드는 검정(black)이고, 같은 저장소의 항목은 child/right 사슬로 잇는다.
    """
    root = _Entry("Root Entry", "root")
    root.kind = "root"
    storages: dict[str, _Entry] = {}
    entries: list[_Entry] = [root]
    for parts, data in streams:
        parent = root
        prefix: list[str] = []
        for part in parts[:-1]:
            key = "/".join(prefix + [part])
            if key not in storages:
                storage = _Entry(part, "storage")
                entries.append(storage)
                storages[key] = storage
                parent.children.append(storage)
            parent = storages[key]
            prefix.append(part)
        stream = _Entry(parts[-1], "stream")
        stream.data = _pad_stream(data)
        # OLE는 4096바이트 미만 스트림을 mini stream에 넣는다. 이 생성기는 mini stream을
        # 쓰지 않으므로 크기를 채운 길이로 적어야 olefile이 일반 섹터에서 읽는다.
        stream.size = len(stream.data)
        entries.append(stream)
        parent.children.append(stream)

    # 같은 저장소의 항목은 이름순으로 child → right 사슬로 잇는다(OLE 탐색 규칙).
    for entry in [e for e in entries if e.children]:
        ordered = sorted(entry.children, key=lambda item: item.name.upper())
        entry.child = entries.index(ordered[0])
        for current, following in zip(ordered, ordered[1:]):
            current.right = entries.index(following)

    dir_sectors = (len(entries) * 128 + SECTOR_SIZE - 1) // SECTOR_SIZE
    stream_sectors = sum(len(e.data) // SECTOR_SIZE for e in entries if e.kind == "stream")
    # FAT 한 엔트리는 512바이트 섹터 하나를 담당한다. 섹터 0은 헤더 바로 뒤이므로
    # 전체 FAT 엔트리 수는 (FAT 섹터 + 디렉터리 섹터 + 스트림 섹터)다.
    fat_sectors = 1
    while True:
        total = fat_sectors + dir_sectors + stream_sectors
        needed = (total + SECTOR_SIZE // 4 - 1) // (SECTOR_SIZE // 4)
        if needed <= fat_sectors:
            break
        fat_sectors = needed

    fat = [FREESECT] * (fat_sectors * (SECTOR_SIZE // 4))
    for index in range(fat_sectors):
        fat[index] = FATSECT
    dir_start = fat_sectors
    for index in range(dir_sectors):
        sector = dir_start + index
        fat[sector] = ENDOFCHAIN if index == dir_sectors - 1 else sector + 1
    next_sector = dir_start + dir_sectors
    bodies: list[bytes] = []
    for entry in entries:
        if entry.kind != "stream":
            continue
        count = len(entry.data) // SECTOR_SIZE
        entry.sector = next_sector
        for index in range(count):
            sector = next_sector + index
            fat[sector] = ENDOFCHAIN if index == count - 1 else sector + 1
        next_sector += count
        bodies.append(entry.data)

    directory = bytearray()
    for entry in entries:
        name = entry.name.encode("utf-16le") + b"\x00\x00"
        block = bytearray(128)
        block[0 : len(name)] = name
        struct.pack_into("<H", block, 64, len(name))
        block[66] = {"root": 5, "storage": 1, "stream": 2}[entry.kind]
        block[67] = 1  # black
        struct.pack_into("<III", block, 68, entry.left, entry.right, entry.child)
        struct.pack_into("<I", block, 116, entry.sector)
        struct.pack_into("<Q", block, 120, entry.size)
        directory += block
    directory = bytes(directory) + b"\x00" * (dir_sectors * SECTOR_SIZE - len(directory))

    header = bytearray(SECTOR_SIZE)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 0x0003)  # major version 3
    struct.pack_into("<H", header, 28, 0xFFFE)  # little endian
    struct.pack_into("<H", header, 30, 9)  # sector shift 512
    struct.pack_into("<H", header, 32, 6)  # mini sector shift 64
    struct.pack_into("<I", header, 44, fat_sectors)
    struct.pack_into("<I", header, 48, dir_start)
    struct.pack_into("<I", header, 56, MINI_CUTOFF)
    struct.pack_into("<I", header, 60, ENDOFCHAIN)  # miniFAT 시작
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, ENDOFCHAIN)  # DIFAT 시작
    struct.pack_into("<I", header, 72, 0)
    for index in range(109):
        struct.pack_into("<I", header, 76 + index * 4, index if index < fat_sectors else FREESECT)

    fat_bytes = b"".join(struct.pack("<I", value) for value in fat)
    fat_bytes = fat_bytes[: fat_sectors * SECTOR_SIZE].ljust(fat_sectors * SECTOR_SIZE, b"\xff")
    return bytes(header) + fat_bytes + directory + b"".join(bodies)


def build_hwp(
    section_records: bytes,
    compressed: bool = True,
    bindata: dict[str, bytes] | None = None,
) -> bytes:
    """FileHeader + BodyText/Section0 + BinData/* 를 담은 최소 HWP 파일 바이트."""
    streams: list[tuple[list[str], bytes]] = [
        (["FileHeader"], file_header(compressed=compressed) + b"\x00" * (MINI_CUTOFF - 256)),
        (["BodyText", "Section0"], section_stream(section_records, compressed)),
    ]
    for name, data in (bindata or {}).items():
        streams.append((["BinData", name], data))
    return build_cfb(streams)


def write_hwp(path, section_records: bytes, compressed: bool = True, bindata: dict[str, bytes] | None = None):
    """build_hwp 결과를 파일로 씀다. 테스트에서 임시 폴더에 만들 때 쓴다."""
    data = build_hwp(section_records, compressed=compressed, bindata=bindata)
    path.write_bytes(data)
    return path
