#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import html
import io
import json
import re
import shutil
import time
import uuid
import zipfile
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import hwp2md


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
SUPPORTED_SUFFIXES = {".hwp", ".hwpx"}
APP_ROOT = Path(__file__).resolve().parent
APP_TMP_ROOT = APP_ROOT / "webapp_tmp"


@dataclasses.dataclass
class UploadedFile:
    field_name: str
    filename: str
    data: bytes


def sanitize_upload_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name or "document.hwp"


def unique_child_path(parent: Path, filename: str) -> Path:
    candidate = parent / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for idx in range(2, 1000):
        next_candidate = parent / f"{stem}-{idx}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    raise hwp2md.ConversionError(f"파일 이름이 너무 많이 겹칩니다: {filename}")


def field_value(fields: dict[str, list[str]], name: str, default: str) -> str:
    values = fields.get(name) or []
    return values[0] if values and values[0] else default


def build_options(fields: dict[str, list[str]]) -> hwp2md.ConvertOptions:
    return hwp2md.ConvertOptions(
        table_mode=field_value(fields, "table_mode", "hybrid"),
        image_mode=field_value(fields, "image_mode", "extract"),
        frontmatter=field_value(fields, "frontmatter", "yaml"),
        include_page_artifacts=field_value(fields, "include_page_artifacts", "") == "on",
    )


def parse_multipart_form(content_type: str, body: bytes) -> tuple[dict[str, list[str]], list[UploadedFile]]:
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("multipart/form-data 요청만 처리할 수 있습니다.")
    raw_message = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    message = BytesParser(policy=default).parsebytes(raw_message)
    fields: dict[str, list[str]] = {}
    files: list[UploadedFile] = []
    if not message.is_multipart():
        raise ValueError("업로드 형식을 읽을 수 없습니다.")
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files.append(UploadedFile(field_name=str(field_name), filename=filename, data=payload))
        else:
            charset = part.get_content_charset() or "utf-8"
            fields.setdefault(str(field_name), []).append(payload.decode(charset, errors="replace"))
    return fields, files


def html_page(message: str | None = None, message_kind: str = "error") -> bytes:
    notice = ""
    if message:
        notice = f'<div class="notice {html.escape(message_kind)}">{html.escape(message)}</div>'
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>hwp2md web</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #5f6b7a;
      --border: #d9e0e8;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --focus: #2563eb;
      --danger-bg: #fff1f2;
      --danger: #be123c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Arial, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .shell {{
      width: min(960px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 20px;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .sub {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 22px;
      box-shadow: 0 10px 28px rgba(31, 41, 55, 0.08);
    }}
    form {{
      display: grid;
      gap: 20px;
    }}
    label {{
      display: block;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    input[type="file"] {{
      width: 100%;
      border: 1px dashed #aeb8c5;
      border-radius: 8px;
      background: #fbfcfe;
      padding: 28px;
      font-size: 15px;
    }}
    .options {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    select {{
      width: 100%;
      height: 40px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 0 10px;
      font-size: 14px;
    }}
    select:focus,
    input:focus {{
      outline: 2px solid var(--focus);
      outline-offset: 2px;
    }}
    .checkline {{
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 14px;
    }}
    .checkline input {{
      width: 18px;
      height: 18px;
    }}
    button {{
      justify-self: start;
      min-width: 150px;
      height: 44px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      font-size: 15px;
      cursor: pointer;
    }}
    button:hover {{ background: var(--accent-strong); }}
    button:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .notice {{
      border-radius: 8px;
      padding: 12px 14px;
      margin-bottom: 16px;
      font-size: 14px;
    }}
    .notice.error {{
      background: var(--danger-bg);
      color: var(--danger);
      border: 1px solid #fecdd3;
    }}
    .files {{
      min-height: 20px;
      color: var(--muted);
      font-size: 14px;
    }}
    footer {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 16px;
    }}
    @media (max-width: 720px) {{
      header {{
        display: block;
      }}
      .options {{
        grid-template-columns: 1fr;
      }}
      .panel {{
        padding: 18px;
      }}
      input[type="file"] {{
        padding: 20px;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>hwp2md</h1>
        <p class="sub">HWP/HWPX 문서를 Markdown zip으로 변환합니다.</p>
      </div>
    </header>
    {notice}
    <section class="panel">
      <form id="convertForm" action="/convert" method="post" enctype="multipart/form-data">
        <div>
          <label for="documents">문서 선택</label>
          <input id="documents" name="documents" type="file" accept=".hwp,.hwpx" multiple required>
          <div id="fileList" class="files"></div>
        </div>
        <div class="options">
          <div>
            <label for="table_mode">표</label>
            <select id="table_mode" name="table_mode">
              <option value="hybrid" selected>자동</option>
              <option value="gfm">Markdown</option>
              <option value="html">HTML</option>
            </select>
          </div>
          <div>
            <label for="image_mode">그림</label>
            <select id="image_mode" name="image_mode">
              <option value="extract" selected>추출</option>
              <option value="skip">건너뛰기</option>
            </select>
          </div>
          <div>
            <label for="frontmatter">문서 정보</label>
            <select id="frontmatter" name="frontmatter">
              <option value="yaml" selected>포함</option>
              <option value="none">제외</option>
            </select>
          </div>
        </div>
        <label class="checkline">
          <input type="checkbox" name="include_page_artifacts">
          머리말/꼬리말 후보도 포함
        </label>
        <button id="submitButton" type="submit">변환 시작</button>
      </form>
    </section>
    <footer>변환은 이 컴퓨터 안에서만 실행됩니다.</footer>
  </main>
  <script>
    const form = document.getElementById("convertForm");
    const input = document.getElementById("documents");
    const fileList = document.getElementById("fileList");
    const button = document.getElementById("submitButton");

    input.addEventListener("change", () => {{
      const names = Array.from(input.files || []).map((file) => file.name);
      fileList.textContent = names.length ? names.join(", ") : "";
    }});

    form.addEventListener("submit", () => {{
      button.disabled = true;
      button.textContent = "변환 중";
    }});
  </script>
</body>
</html>
""".encode("utf-8")


def write_zip(output_root: Path, summary: list[dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        for path in sorted(output_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_root).as_posix())
    return buffer.getvalue()


class Hwp2MdRequestHandler(BaseHTTPRequestHandler):
    server_version = "hwp2md-web/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_text("ok\n", "text/plain; charset=utf-8")
            return
        if path in {"/", "/index.html"}:
            self.send_html(html_page())
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/convert":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            self.handle_convert()
        except hwp2md.ConversionError as exc:
            self.send_html(html_page(str(exc)), status=HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self.send_html(html_page(str(exc)), status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_html(html_page(f"변환 중 오류가 발생했습니다: {exc}"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_convert(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            raise ValueError("업로드된 파일이 없습니다.")
        if content_length > MAX_UPLOAD_BYTES:
            raise ValueError("파일이 너무 큽니다. 한 번에 100MB 이하로 올려 주세요.")

        body = self.rfile.read(content_length)
        fields, uploaded_files = parse_multipart_form(self.headers.get("Content-Type", ""), body)
        options = build_options(fields)
        file_items = [item for item in uploaded_files if item.field_name == "documents" and item.filename]
        if not file_items:
            raise ValueError("변환할 HWP/HWPX 파일을 선택해 주세요.")

        APP_TMP_ROOT.mkdir(exist_ok=True)
        temp_dir = APP_TMP_ROOT / f"job-{uuid.uuid4().hex}"
        temp_dir.mkdir()
        try:
            input_dir = temp_dir / "uploads"
            output_root = temp_dir / "converted"
            input_dir.mkdir()
            output_root.mkdir()
            summary: list[dict[str, Any]] = []

            for item in file_items:
                original_name = sanitize_upload_filename(item.filename or "document.hwp")
                if Path(original_name).suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise ValueError(f"지원하지 않는 파일입니다: {original_name}")
                input_path = unique_child_path(input_dir, original_name)
                input_path.write_bytes(item.data)

                stem = hwp2md.source_stem(input_path)
                md_path = output_root / f"{stem}.md"
                assets_dir = output_root / f"{stem}.assets"
                report_path = output_root / f"{stem}.report.json"
                started = time.time()
                result = hwp2md.convert_file(input_path, md_path, assets_dir, report_path, options)
                summary.append(
                    {
                        "source": original_name,
                        "markdown": md_path.name,
                        "report": report_path.name,
                        "assets": len(result.assets),
                        "warnings": len(result.report.get("warnings", [])),
                        "losses": len(result.report.get("losses", [])),
                        "elapsedSeconds": round(time.time() - started, 3),
                    }
                )

            zip_data = write_zip(output_root, summary)
            filename = "hwp2md-result.zip"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(zip_data)))
            self.end_headers()
            self.wfile.write(zip_data)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def send_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="hwp2md local web app")
    parser.add_argument("--host", default="127.0.0.1", help="서버 주소")
    parser.add_argument("--port", type=int, default=8765, help="서버 포트")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Hwp2MdRequestHandler)
    print(f"hwp2md web app: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
