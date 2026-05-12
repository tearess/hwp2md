from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import webapp  # noqa: E402


class handler(webapp.Hwp2MdRequestHandler):
    def do_POST(self) -> None:
        _route, sep, query = self.path.partition("?")
        self.path = "/convert" + (sep + query if sep else "")
        super().do_POST()
