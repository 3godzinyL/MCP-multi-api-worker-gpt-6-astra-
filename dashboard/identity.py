import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL_ID = hashlib.sha256(str(ROOT).casefold().encode("utf-8")).hexdigest()[:20]
