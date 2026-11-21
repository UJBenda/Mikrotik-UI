"""Překlady rozhraní: úplnost anglického slovníku a API slovníků."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import i18n  # noqa: E402


def test_english_is_complete():
    pr = i18n.problems("en")
    assert pr["missing"] == [], f"chybí anglický překlad: {pr['missing'][:10]} (tools/i18n.py sync en)"
    assert pr["placeholders"] == [], f"překlad nemá stejné {{n}} jako zdroj: {pr['placeholders'][:10]}"
    assert pr["extra"] == [], f"přebytečné klíče: {pr['extra'][:10]} (tools/i18n.py sync en)"
    d = i18n.load("en")
    assert d["code"] == "en" and d["language"] == "English" and len(d["strings"]) > 1500
    # mezery na krajích se musí zachovat (texty se skládají)
    bad = [k for k, v in d["strings"].items() if (k[:1] == " ") != (v[:1] == " ") or (k[-1:] == " ") != (v[-1:] == " ")]
    assert bad == [], bad[:10]
    # šablona pro nové jazyky odpovídá zdrojovým textům
    tpl = json.loads((i18n.I18N / "template.json").read_text(encoding="utf-8"))
    assert list(tpl["strings"]) == i18n.source_keys(), "spusť tools/i18n.py template"


def test_extraction():
    js = 'el("b", __("Uložit")); msg(__`Smazat ${a.name}? ${n > 1 ? __("vše") : `x${y}`}`); __(dyn); x = `ne ${z}`;'
    assert i18n.js_keys(js) == ["Uložit", "Smazat {0}? {1}", "vše"]
    assert i18n.js_keys(r'__("a\"b\n"); __`řádek\n${x}`') == ['a"b\n', "řádek\n{0}"]
    html = '<p title="Nápověda">Text <b>tučně</b>\n  dál</p><script>var x = "ne";</script><input placeholder="Hledat…">'
    assert i18n.html_keys(html) == ["Nápověda", "Text", "tučně", "dál", "Hledat…"]


def test_api_and_custom_dictionary(tmp_path):
    from helpers import AsgiClient, drop_db
    from test_modules import _env

    db, name, settings, box, app, tid, sid = _env()
    try:
        c = AsgiClient(app, "admin.test")
        r = c.get("/i18n/index.json")
        assert r.status_code == 200
        langs = {x["code"]: x["language"] for x in r.json()["languages"]}
        assert langs["cs"] == "Čeština" and langs["en"] == "English" and "template" not in langs
        en = c.get("/i18n/en.json").json()
        assert en["strings"]["Uložit"] == "Save" and en["locale"] == "en-GB"
        for bad in ("xx.json", "template.json", "..%2Fen.json", "en", "EN.json"):
            assert c.get(f"/i18n/{bad}").status_code == 404, bad
        # vlastní slovník v <data_dir>/i18n: nový jazyk a přepsání jednotlivých textů
        object.__setattr__(settings.paths, "data_dir", tmp_path)
        d = tmp_path / "i18n"
        d.mkdir()
        (d / "de.json").write_text(json.dumps({"language": "Deutsch", "code": "de", "strings": {"Uložit": "Speichern", "x": ""}}))
        (d / "en.json").write_text(json.dumps({"strings": {"Lokalita": "Location"}}))
        (d / "zz.json").write_text("{neplatné")
        langs = {x["code"]: x["language"] for x in c.get("/i18n/index.json").json()["languages"]}
        assert langs.get("de") == "Deutsch" and "zz" not in langs
        assert c.get("/i18n/de.json").json()["strings"] == {"Uložit": "Speichern"}
        en = c.get("/i18n/en.json").json()
        assert en["strings"]["Lokalita"] == "Location" and en["strings"]["Uložit"] == "Save" and en["language"] == "English"
    finally:
        drop_db(name)
