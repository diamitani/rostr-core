import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from server import Handler as Base

class handler(Base):
    def do_POST(self):
        self.path = "/v1/generate"
        return super().do_POST()
    def do_OPTIONS(self):
        return super().do_OPTIONS()
