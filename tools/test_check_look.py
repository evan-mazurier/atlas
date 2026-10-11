"""tools/check_look.py: the colour-blind and contrast rules (docs/v2/contracts.md §4.1).

Run:  py -3.12 -m pytest -q tools/test_check_look.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import check_look as cl  # noqa: E402

APPROVED = {"md": "#d95926", "job": "#199e70", "train": "#c98500", "atlas": "#9085e9", "admin": "#008300", "pers": "#d55181"}


def areas_yaml(tmp_path, colors, extra=""):
    lines = ["areas:"]
    for aid, col in colors.items():
        lines += [f"  - id: {aid}", f"    name: Area {aid}", f'    color: "{col}"']
    p = tmp_path / "areas.yaml"
    p.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
    return p


def test_contrast_ratio_extremes():
    assert cl.contrast("#ffffff", "#000000") == pytest.approx(21.0)
    assert cl.contrast("#000000", "#000000") == pytest.approx(1.0)


def test_the_repo_areas_file_passes():
    assert cl.check_areas(ROOT / "areas.yaml") == []


def test_the_six_approved_colours_pass(tmp_path):
    assert cl.check_areas(areas_yaml(tmp_path, APPROVED)) == []


def test_reference_numbers_for_the_approved_six():
    pal = list(APPROVED.values())
    worst_cvd = min(cl.delta_e(a, b, k) for k in ("protan", "deutan") for a, b in zip(pal, pal[1:]))
    worst_normal = min(cl.delta_e(a, b) for a, b in zip(pal, pal[1:]))
    assert worst_cvd == pytest.approx(8.4, abs=0.05) and worst_normal == pytest.approx(19.8, abs=0.05)
    assert set(cl.validate_palette(pal).values()) == {"pass"}


def test_two_near_identical_adjacent_colours_fail(tmp_path):
    bad = {"md": "#d95926", "pers": "#d95a27", "job": "#199e70"}
    problems = cl.check_areas(areas_yaml(tmp_path, bad))
    assert any("md and pers" in p and "normal vision" in p for p in problems), problems


def test_an_adjacent_pair_that_collapses_for_colour_blind_readers_fails(tmp_path):
    # red and green of similar lightness: apart in normal vision, together under protan/deutan
    problems = cl.check_areas(areas_yaml(tmp_path, {"red": "#d95926", "green": "#6a9a1f"}))
    assert any("colour-blind" in p for p in problems), problems


def test_a_colour_outside_the_lightness_band_fails(tmp_path):
    assert any("lightness band" in p for p in cl.check_areas(areas_yaml(tmp_path, {"pale": "#e8e8ff", "mid": "#d95926"})))


def test_a_gray_fails_the_chroma_floor(tmp_path):
    assert any("chroma" in p for p in cl.check_areas(areas_yaml(tmp_path, {"gray": "#808080", "mid": "#d95926"})))


VENDOR = HERE / "vendor" / "validate_palette.js"
PARITY_SETS = [
    list(APPROVED.values()),
    ["#d95926", "#d95a27", "#199e70"],
    ["#d95926", "#6a9a1f", "#9085e9"],
    ["#e8e8ff", "#d95926", "#808080"],
    ["#202020", "#d95926", "#199e70"],
    ["#3987e5", "#2a6fc4", "#86b6ef", "#d03b3b"],
    ["#c98500", "#199e70", "#d55181", "#9085e9"],
]
STATE = {"PASS": {"pass"}, "WARN": {"floor", "relief"}, "FAIL": {"fail"}}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("palette", PARITY_SETS, ids=lambda p: ",".join(p)[:30])
def test_python_port_matches_the_vendored_validator(palette):
    r = subprocess.run(["node", str(VENDOR), ",".join(palette), "--mode", "dark", "--surface", "#000000"], capture_output=True, text=True)
    js = {}
    for line in r.stdout.splitlines():
        m = re.match(r"\s*\[(PASS|WARN|FAIL)\]\s+(Lightness band|Chroma floor|CVD separation|Normal-vision floor|Contrast vs surface)", line)
        if m:
            js[m.group(2)] = m.group(1)
    assert len(js) == 5, r.stdout
    py = cl.validate_palette(palette)
    names = {"lightness": "Lightness band", "chroma": "Chroma floor", "cvd": "CVD separation", "normal": "Normal-vision floor", "contrast": "Contrast vs surface"}
    for key, label in names.items():
        assert py[key] in STATE[js[label]], (key, py[key], js[label])
    assert (r.returncode == 0) == ("fail" not in py.values())


def test_low_contrast_colour_fails(tmp_path):
    problems = cl.check_areas(areas_yaml(tmp_path, {"dim": "#202020"}))
    assert any("contrast" in p for p in problems)


@pytest.mark.parametrize("ident", ["Bad", "1abc", "has space", "waytoolongidentifier1"])
def test_bad_ids_fail(tmp_path, ident):
    p = tmp_path / "areas.yaml"
    p.write_text(f'areas:\n  - id: "{ident}"\n    name: X\n    color: "#d95926"\n', encoding="utf-8")
    assert any("id" in x for x in cl.check_areas(p))


def test_duplicate_ids_empty_names_and_bad_hex_fail(tmp_path):
    p = tmp_path / "areas.yaml"
    p.write_text('areas:\n  - {id: a, name: "", color: "#d95926"}\n  - {id: a, name: B, color: "orange"}\n', encoding="utf-8")
    problems = cl.check_areas(p)
    assert any("duplicate" in x for x in problems)
    assert any("name is empty" in x for x in problems)
    assert any("#rrggbb" in x for x in problems)


def test_unparseable_yaml_fails(tmp_path):
    p = tmp_path / "areas.yaml"
    p.write_text("areas: [unclosed", encoding="utf-8")
    assert any("does not parse" in x for x in cl.check_areas(p))


def test_the_repo_look_css_passes():
    assert cl.check_look(ROOT / "cockpit" / "web2" / "look.css") == []


def good_css():
    return (ROOT / "cockpit" / "web2" / "look.css").read_text(encoding="utf-8")


def test_look_css_with_a_second_selector_fails(tmp_path):
    p = tmp_path / "look.css"
    p.write_text(good_css() + "\nbody{color:red}\n", encoding="utf-8")
    assert any("may only contain :root" in x for x in cl.check_look(p))


def test_look_css_missing_token_and_bad_hex_fail(tmp_path):
    p = tmp_path / "look.css"
    p.write_text(good_css().replace("--crit:#d03b3b;", "").replace("--warn:#fab219", "--warn:#fab"), encoding="utf-8")
    problems = cl.check_look(p)
    assert any("missing token --crit" in x for x in problems)
    assert any("--warn" in x and "hex" in x for x in problems)


def test_look_css_low_contrast_ink_and_muted_fail(tmp_path):
    p = tmp_path / "look.css"
    p.write_text(good_css().replace("--ink:#ededed", "--ink:#808080").replace("--muted:#8f8f8f", "--muted:#444444"), encoding="utf-8")
    problems = cl.check_look(p)
    assert any("--ink on --bg" in x for x in problems)
    assert any("--muted on --bg" in x for x in problems)


def run_cli(*args):
    return subprocess.run([sys.executable, str(HERE / "check_look.py"), *args], capture_output=True, text=True)


def test_cli_exit_codes_and_json(tmp_path):
    ok = run_cli("--json", str(ROOT / "areas.yaml"))
    assert ok.returncode == 0 and json.loads(ok.stdout) == {"ok": True, "problems": []}
    bad = run_cli("--json", str(areas_yaml(tmp_path, {"dim": "#202020"})))
    out = json.loads(bad.stdout)
    assert bad.returncode == 1 and out["ok"] is False and out["problems"]
    assert run_cli("--json", str(tmp_path / "missing.yaml")).returncode == 1
