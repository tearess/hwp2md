#!/usr/bin/env python3
"""Small desktop GUI for hwp2md."""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import hwp2md


APP_ROOT = Path(__file__).resolve().parent
SUPPORTED_SUFFIXES = {".hwp", ".hwpx"}


class Hwp2MdGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.files: list[Path] = []
        self.file_keys: set[str] = set()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None

        self.output_dir = tk.StringVar(value=str(APP_ROOT / "converted"))
        self.recursive = tk.BooleanVar(value=True)
        self.table_mode = tk.StringVar(value="hybrid")
        self.image_mode = tk.StringVar(value="extract")
        self.frontmatter = tk.StringVar(value="yaml")
        self.clean_assets = tk.BooleanVar(value=True)
        self.include_page_artifacts = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="HWP/HWPX 파일을 추가해 주세요.")

        self.root.title("hwp2md")
        self.root.geometry("860x620")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.root.after(100, self.poll_events)

    def build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        main.rowconfigure(5, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(5, weight=1)

        self.add_files_button = ttk.Button(header, text="파일 추가", command=self.add_files)
        self.add_files_button.grid(row=0, column=0, padx=(0, 6))
        self.add_folder_button = ttk.Button(header, text="폴더 추가", command=self.add_folder)
        self.add_folder_button.grid(row=0, column=1, padx=(0, 6))
        self.remove_button = ttk.Button(header, text="선택 삭제", command=self.remove_selected)
        self.remove_button.grid(row=0, column=2, padx=(0, 6))
        self.clear_button = ttk.Button(header, text="전체 삭제", command=self.clear_files)
        self.clear_button.grid(row=0, column=3, padx=(0, 12))
        ttk.Checkbutton(header, text="하위 폴더 포함", variable=self.recursive).grid(row=0, column=4, sticky="w")

        list_frame = ttk.Frame(main)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.file_list = tk.Listbox(list_frame, selectmode=tk.EXTENDED, activestyle="none")
        self.file_list.grid(row=0, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_list.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.file_list.configure(yscrollcommand=list_scroll.set)

        output_frame = ttk.LabelFrame(main, text="출력")
        output_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        output_frame.columnconfigure(1, weight=1)
        ttk.Label(output_frame, text="결과 폴더").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Entry(output_frame, textvariable=self.output_dir).grid(row=0, column=1, padx=(0, 6), pady=8, sticky="ew")
        self.browse_button = ttk.Button(output_frame, text="찾기", command=self.choose_output_dir)
        self.browse_button.grid(row=0, column=2, padx=(0, 6), pady=8)
        self.open_button = ttk.Button(output_frame, text="결과 열기", command=self.open_output_dir)
        self.open_button.grid(row=0, column=3, padx=(0, 8), pady=8)

        options_frame = ttk.LabelFrame(main, text="변환 옵션")
        options_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for col in range(8):
            options_frame.columnconfigure(col, weight=1 if col in {1, 3, 5} else 0)

        ttk.Label(options_frame, text="표").grid(row=0, column=0, padx=(8, 4), pady=8, sticky="w")
        ttk.Combobox(
            options_frame,
            textvariable=self.table_mode,
            values=("hybrid", "gfm", "html"),
            state="readonly",
            width=12,
        ).grid(row=0, column=1, padx=(0, 12), pady=8, sticky="w")
        ttk.Label(options_frame, text="이미지").grid(row=0, column=2, padx=(0, 4), pady=8, sticky="w")
        ttk.Combobox(
            options_frame,
            textvariable=self.image_mode,
            values=("extract", "skip"),
            state="readonly",
            width=12,
        ).grid(row=0, column=3, padx=(0, 12), pady=8, sticky="w")
        ttk.Label(options_frame, text="frontmatter").grid(row=0, column=4, padx=(0, 4), pady=8, sticky="w")
        ttk.Combobox(
            options_frame,
            textvariable=self.frontmatter,
            values=("yaml", "none"),
            state="readonly",
            width=12,
        ).grid(row=0, column=5, padx=(0, 12), pady=8, sticky="w")
        ttk.Checkbutton(options_frame, text="기존 assets 정리", variable=self.clean_assets).grid(
            row=0, column=6, padx=(0, 12), pady=8, sticky="w"
        )
        ttk.Checkbutton(options_frame, text="머리말/꼬리말 포함", variable=self.include_page_artifacts).grid(
            row=0, column=7, padx=(0, 8), pady=8, sticky="w"
        )

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(1, weight=1)
        self.convert_button = ttk.Button(actions, text="변환 시작", command=self.start_conversion)
        self.convert_button.grid(row=0, column=0, padx=(0, 10))
        self.progress = ttk.Progressbar(actions, mode="indeterminate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(actions, textvariable=self.status).grid(row=0, column=2, sticky="e")

        log_frame = ttk.LabelFrame(main, text="기록")
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="HWP/HWPX 파일 선택",
            filetypes=(("HWP/HWPX 문서", "*.hwp *.hwpx"), ("모든 파일", "*.*")),
        )
        self.add_paths(Path(path) for path in paths)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="HWP/HWPX 파일이 있는 폴더 선택")
        if not folder:
            return
        root = Path(folder)
        iterator = root.rglob("*") if self.recursive.get() else root.iterdir()
        self.add_paths(path for path in iterator if path.is_file())

    def add_paths(self, paths: object) -> None:
        added = 0
        skipped = 0
        for raw_path in paths:
            path = Path(raw_path)
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                skipped += 1
                continue
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                skipped += 1
                continue
            key = str(resolved).casefold()
            if key in self.file_keys:
                continue
            self.files.append(resolved)
            self.file_keys.add(key)
            self.file_list.insert(tk.END, str(resolved))
            added += 1
        if added:
            self.status.set(f"{len(self.files)}개 파일이 목록에 있습니다.")
        elif skipped:
            self.status.set("지원하는 .hwp/.hwpx 파일을 찾지 못했습니다.")

    def remove_selected(self) -> None:
        selected = list(self.file_list.curselection())
        if not selected:
            return
        for index in reversed(selected):
            path = self.files.pop(index)
            self.file_keys.discard(str(path).casefold())
            self.file_list.delete(index)
        self.status.set(f"{len(self.files)}개 파일이 목록에 있습니다.")

    def clear_files(self) -> None:
        self.files.clear()
        self.file_keys.clear()
        self.file_list.delete(0, tk.END)
        self.status.set("목록을 비웠습니다.")

    def choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="결과 폴더 선택", initialdir=self.output_dir.get())
        if folder:
            self.output_dir.set(folder)

    def open_output_dir(self) -> None:
        path = Path(self.output_dir.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("결과 폴더 열기 실패", str(exc))

    def start_conversion(self) -> None:
        if self.is_busy():
            return
        if not self.files:
            messagebox.showinfo("파일 없음", "먼저 HWP/HWPX 파일을 추가해 주세요.")
            return
        output_dir = Path(self.output_dir.get()).expanduser()
        files = list(self.files)
        options = hwp2md.ConvertOptions(
            table_mode=self.table_mode.get(),
            image_mode=self.image_mode.get(),
            frontmatter=self.frontmatter.get(),
            include_page_artifacts=self.include_page_artifacts.get(),
            clean_assets=self.clean_assets.get(),
        )

        self.set_busy(True)
        self.progress.start(12)
        self.status.set("변환 중입니다...")
        self.append_log(f"변환 시작: {len(files)}개 파일")
        self.worker = threading.Thread(
            target=self.convert_worker,
            args=(files, output_dir, options),
            daemon=True,
        )
        self.worker.start()

    def convert_worker(self, files: list[Path], output_dir: Path, options: hwp2md.ConvertOptions) -> None:
        ok = 0
        failed = 0
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.events.put(("fatal", f"결과 폴더를 만들 수 없습니다: {exc}"))
            return

        for index, input_path in enumerate(files, start=1):
            self.events.put(("status", f"{index}/{len(files)} 변환 중: {input_path.name}"))
            try:
                md_path = hwp2md.unique_path(output_dir / f"{hwp2md.source_stem(input_path)}.md")
                result = hwp2md.convert_file(input_path, md_path, None, None, options)
                warnings = len(result.report.get("warnings", []))
                losses = len(result.report.get("losses", []))
                assets = len(result.assets)
                self.events.put(
                    (
                        "log",
                        f"OK  {input_path.name} -> {md_path.name} "
                        f"(assets {assets}, warnings {warnings}, losses {losses})",
                    )
                )
                ok += 1
            except Exception as exc:
                failed += 1
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                self.events.put(("log", f"FAIL {input_path.name}: {detail}"))

        self.events.put(("done", {"ok": ok, "failed": failed, "output_dir": output_dir}))

    def poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(str(payload))
            elif kind == "log":
                self.append_log(str(payload))
            elif kind == "fatal":
                self.finish_busy()
                self.append_log(str(payload))
                self.status.set("변환 실패")
                messagebox.showerror("변환 실패", str(payload))
            elif kind == "done":
                data = payload if isinstance(payload, dict) else {}
                self.finish_busy()
                ok = data.get("ok", 0)
                failed = data.get("failed", 0)
                output_dir = data.get("output_dir", Path(self.output_dir.get()))
                self.status.set(f"완료: 성공 {ok}개, 실패 {failed}개")
                self.append_log(f"변환 완료: 성공 {ok}개, 실패 {failed}개, 결과 폴더 {output_dir}")
                if failed:
                    messagebox.showwarning("변환 완료", f"성공 {ok}개, 실패 {failed}개입니다. 기록을 확인해 주세요.")
                else:
                    messagebox.showinfo("변환 완료", f"{ok}개 파일 변환이 끝났습니다.")
        self.root.after(100, self.poll_events)

    def append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (
            self.add_files_button,
            self.add_folder_button,
            self.remove_button,
            self.clear_button,
            self.browse_button,
            self.convert_button,
        ):
            button.configure(state=state)

    def finish_busy(self) -> None:
        self.progress.stop()
        self.set_busy(False)
        self.worker = None

    def is_busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def on_close(self) -> None:
        if self.is_busy() and not messagebox.askyesno("종료", "변환 중입니다. 그래도 종료할까요?"):
            return
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    Hwp2MdGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
