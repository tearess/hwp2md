from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import webapp  # noqa: E402


class handler(webapp.Hwp2MdRequestHandler):
    def do_GET(self) -> None:
        route, sep, query = self.path.partition("?")
        if route in {"/api", "/api/", "/api/index", "/api/index.py"}:
            self.path = "/" + (sep + query if sep else "")
        super().do_GET()
