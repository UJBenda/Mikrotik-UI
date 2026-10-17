"""Spustí admin UI (app.js) v Node s minimálním DOM nad zachycenými odpověďmi API.
Odhalí chyby vykreslení (chybějící prvky, výjimky, použití innerHTML)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "mtcontroller" / "web" / "static"


def test_ui_renders_without_errors():
    node = shutil.which("node")
    if not node:
        pytest.skip("node není k dispozici")
    r = subprocess.run([node, str(ROOT / "tests/ui/uitest.js"), str(STATIC / "app.js"), str(STATIC / "index.html"),
                        str(ROOT / "tests/ui/api-fixture.json")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "UI OK" in r.stdout


def test_ui_never_uses_innerhtml():
    import re

    js = (STATIC / "app.js").read_text()
    assert not re.search(r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write", js)
