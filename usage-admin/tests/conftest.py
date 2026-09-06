"""Test bootstrap: expose the usage-admin test support package on sys.path."""



import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
