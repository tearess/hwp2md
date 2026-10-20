import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hwp2md
import hwp2md_gui


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "170508(조간) 16년말 외국인이 보유한 국내 토지는 233제곱키로미터 전 국토의 0.2퍼센트(토지정책과).hwp"


def drain(events: "queue.Queue[tuple[str, object]]") -> list[tuple[str, object]]:
    items = []
    while not events.empty():
        items.append(events.get_nowait())
    return items


class GuiWorkerTests(unittest.TestCase):
    def run_worker(self, files, out_dir):
        stub = SimpleNamespace(events=queue.Queue())
        hwp2md_gui.Hwp2MdGui.convert_worker(stub, files, out_dir, hwp2md.ConvertOptions())
        return drain(stub.events)

    def test_worker_converts_sample_and_reports_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = self.run_worker([SAMPLE], Path(tmp) / "out")
            done = [payload for kind, payload in events if kind == "done"]
            self.assertEqual(done[0]["ok"], 1)
            self.assertEqual(done[0]["failed"], 0)
            self.assertEqual(len(list((Path(tmp) / "out").glob("*.md"))), 1)

    def test_worker_logs_failure_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.hwp"
            bad.write_bytes(b"not a hwp file")
            events = self.run_worker([bad, SAMPLE], Path(tmp) / "out")
            logs = [payload for kind, payload in events if kind == "log"]
            self.assertTrue(any(str(line).startswith("FAIL broken.hwp") for line in logs))
            done = [payload for kind, payload in events if kind == "done"][0]
            self.assertEqual((done["ok"], done["failed"]), (1, 1))

    def test_worker_reports_unwritable_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file.txt"
            blocker.write_text("x")
            events = self.run_worker([SAMPLE], blocker / "sub")
            self.assertEqual(events[0][0], "fatal")


class PackagedAppTests(unittest.TestCase):
    def test_default_output_dir_when_run_from_source(self) -> None:
        self.assertEqual(hwp2md_gui.default_output_dir(), hwp2md_gui.APP_ROOT / "converted")

    def test_default_output_dir_when_frozen(self) -> None:
        # 실행 파일로 묶이면 프로그램 폴더가 임시 압축 해제 폴더이므로 결과를 거기에 두면 사라진다.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hwp2md_gui.sys, "frozen", True, create=True), mock.patch.object(hwp2md_gui.Path, "home", return_value=Path(tmp)):
            (Path(tmp) / "Documents").mkdir()
            self.assertEqual(hwp2md_gui.default_output_dir(), Path(tmp) / "Documents" / "hwp2md 변환결과")

    def test_dropped_paths_expand_folders(self) -> None:
        # exe 아이콘에 파일이나 폴더를 끌어다 놓으면 그 경로가 명령줄 인수로 들어온다.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            for name in ("a.hwp", "sub/b.hwpx", "sub/c.txt"):
                (root / name).write_bytes(b"x")
            found = hwp2md_gui.expand_input_paths([str(root / "a.hwp"), str(root / "sub")])
        self.assertEqual(sorted(p.name for p in found), ["a.hwp", "b.hwpx", "c.txt"])


if __name__ == "__main__":
    unittest.main()
