from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sqlite3


data_root = Path(os.environ.get("FRAMEPILOT_DATA_ROOT", "/srv/framepilot"))
source_path = data_root / "db" / "framepilot.sqlite3"
backup_root = data_root / "backups"
backup_root.mkdir(parents=True, exist_ok=True)
stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
destination = backup_root / f"framepilot-{stamp}.sqlite3"

with sqlite3.connect(source_path) as source:
    with sqlite3.connect(destination) as target:
        source.backup(target)

cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - 14 * 86400
for candidate in backup_root.glob("framepilot-*.sqlite3"):
    try:
        if candidate.stat().st_mtime < cutoff:
            candidate.unlink()
    except OSError:
        pass
