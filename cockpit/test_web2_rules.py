"""House rules for the v2 front end (docs/v2/contracts.md §4, §8, lane brief).

* no hex colour literal outside look.css, areas.yaml, and the marked creature code
* no native form controls (select, checkbox, radio, date, time, number, range, color, file)
* no em-dash anywhere in the front end
* look.css passes tools/check_look.py
* pure JS helpers pass `node --test` (when node is installed)

Run:  py -3.12 -m pytest -q cockpit/test_web2_rules.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WEB2 = HERE / "web2"
sys.path.insert(0, str(ROOT / "tools"))

SOURCES = sorted(p for p in WEB2.rglob("*") if p.is_file() and p.suffix in {".js", ".css", ".html"})
HEX = re.compile(r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{4}\b|#[0-9a-fA-F]{3}\b")
CREATURES = re.compile(r"/\* creatures: verbatim from v1 \*/.*?/\* end creatures \*/", re.S)
CREATURE_FILES = {"team-creatures.js", "team.css"}


def test_there_is_something_to_check():
    assert any(p.name == "app.js" for p in SOURCES) and any(p.name == "base.css" for p in SOURCES)


def strip_creatures(p: Path, text: str) -> str:
    return CREATURES.sub("", text) if p.name in CREATURE_FILES else text


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(WEB2).as_posix())
def test_no_hex_literal_outside_look_css_and_creatures(path):
    if path.name == "look.css":
        return
    text = strip_creatures(path, path.read_text(encoding="utf-8"))
    found = HEX.findall(text)
    assert not found, f"{path.name}: hex literals {found[:5]}"


def test_creature_markers_are_present_and_paired():
    for name in CREATURE_FILES:
        p = next(s for s in SOURCES if s.name == name)
        text = p.read_text(encoding="utf-8")
        assert text.count("/* creatures: verbatim from v1 */") == 1 and text.count("/* end creatures */") == 1
        assert CREATURES.search(text), name


def test_creature_code_is_copied_from_v1():
    v1 = (HERE / "fixtures" / "v1_team.html").read_text(encoding="utf-8")
    js = next(s for s in SOURCES if s.name == "team-creatures.js").read_text(encoding="utf-8")
    block = CREATURES.search(js).group(0)
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("/*") or not stripped:
            continue
        assert stripped in v1, f"not verbatim from v1: {stripped[:80]}"


NATIVE = [
    re.compile(r"<\s*select\b", re.I),
    re.compile(r"createElement\(\s*['\"](select|datalist)['\"]", re.I),
    re.compile(r"""type\s*=\s*["']?(checkbox|radio|date|time|datetime-local|month|week|number|range|color|file)\b""", re.I),
    re.compile(r"""\.type\s*=\s*["'](checkbox|radio|date|time|datetime-local|month|week|number|range|color|file)["']""", re.I),
    re.compile(r"<\s*(textarea|datalist)\b", re.I),
]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(WEB2).as_posix())
def test_no_native_form_controls(path):
    text = path.read_text(encoding="utf-8")
    for rx in NATIVE:
        m = rx.search(text)
        assert not m, f"{path.name}: native control {m.group(0)!r}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(WEB2).as_posix())
def test_no_em_dash(path):
    assert "—" not in path.read_text(encoding="utf-8"), path.name


def test_look_css_passes_the_look_check():
    import check_look
    assert check_look.check_look(WEB2 / "look.css") == []


def test_look_css_is_tokens_only_and_other_css_never_defines_tokens():
    import check_look
    tokens, problems = check_look.parse_css_tokens((WEB2 / "look.css").read_text(encoding="utf-8"))
    assert problems == [] and not any(t.startswith("--a-") for t in tokens)
    for p in SOURCES:
        if p.suffix == ".css" and p.name != "look.css":
            assert not re.search(r":root\s*\{", p.read_text(encoding="utf-8")), p.name


def test_every_stylesheet_in_the_folder_is_linked_from_index():
    index = (WEB2 / "index.html").read_text(encoding="utf-8")
    for p in WEB2.rglob("*.css"):
        assert p.relative_to(WEB2).as_posix() in index, p.name


def test_top_bar_has_no_usage_window_meter():
    index = (WEB2 / "index.html").read_text(encoding="utf-8")
    app = (WEB2 / "app.js").read_text(encoding="utf-8")
    base = (WEB2 / "base.css").read_text(encoding="utf-8")
    for gone in ("ab-segm", "ab-win", "Window", 'class="um"'):
        assert gone not in index, gone
    for kept in ("ab-working-t", "ab-waiting-t"):          # the other chips stay
        assert kept in index and kept in app
    assert "ab-review" not in index and "ab-review" not in app          # the pending-review chip is gone (2026-10-15)
    assert "a.window" not in app and "segMeter" not in app
    assert not re.search(r"\.segm[ {,]", base) and ".um{" not in base
    assert "segMeter" not in (WEB2 / "kit" / "charts.js").read_text(encoding="utf-8")


def test_screens_export_the_contract_shape():
    for sid in ("plan", "team", "ledger"):
        text = (WEB2 / "screens" / f"{sid}.js").read_text(encoding="utf-8")
        assert "export default" in text and f'id: "{sid}"' in text and "mount(" in text and "unmount(" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_unit_tests_for_the_pure_helpers():
    files = ["test_web2_format.mjs", "test_web2_bypass.mjs", "test_web2_integrity.mjs", "test_web2_plan.mjs", "test_web2_ledger.mjs"]
    r = subprocess.run(["node", "--test"] + [str(HERE / f) for f in files], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1000:]


def test_ledger_screen_never_plots_a_null_as_zero():
    src = (WEB2 / "screens" / "ledger.js").read_text(encoding="utf-8")
    assert "kit.spark(t, (d.spark || []).filter((v) => v != null))" in src
    assert "fp.filter((x) => x.pct != null)" in src
    assert 'last == null ? "–"' in src
