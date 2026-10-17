"""Stav workeru: po každém kole zapíše JSON do datového adresáře (atomicky).
Web ho ukazuje superadminovi, `mtc-admin doctor` kontroluje jeho stáří."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

FILE = "worker-status.json"
STALE_AFTER = 180  # s – worker má kolo každých 30 s


def write(data_dir: Path, status: dict[str, Any]) -> None:
    path = Path(data_dir) / FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, default=str, ensure_ascii=False))
    os.replace(tmp, path)


def read(data_dir: Path) -> dict[str, Any] | None:
    try:
        st = json.loads((Path(data_dir) / FILE).read_text())
    except (OSError, ValueError):
        return None
    st["age_seconds"] = round(time.time() - float(st.get("at_epoch") or 0))
    st["stale"] = st["age_seconds"] > STALE_AFTER
    return st
