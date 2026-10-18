import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
