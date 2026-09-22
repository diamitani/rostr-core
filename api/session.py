import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from server import Handler as Base

from urllib.parse import parse_qs, urlparse

class handler(Base):
    def _id(self):
        return (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
    def do_GET(self):
        self.path = f"/v1/sessions/{self._id()}"
        return super().do_GET()
    def do_POST(self):
        self.path = f"/v1/sessions/{self._id()}"
        return super().do_POST()
    def do_OPTIONS(self):
        return super().do_OPTIONS()
