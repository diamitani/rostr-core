import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from server import Handler as Base

class handler(Base):
    def do_GET(self):
        self.path = "/v1/health"
        return super().do_GET()
    def do_OPTIONS(self):
        return super().do_OPTIONS()
