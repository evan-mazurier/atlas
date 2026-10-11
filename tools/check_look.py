"""The automatic look check (docs/v2/contracts.md §4.1).

    py -3.12 tools/check_look.py [--json] <areas.yaml|look.css> [...]

Exit 0 when every file passes, 1 otherwise. `--json` prints {"ok": bool, "problems": [...]}.

areas.yaml : parses; ids unique and ^[a-z][a-z0-9-]{0,15}$; names non-empty; colours #rrggbb; the colours, in
             file order, pass the five checks of tools/vendor/validate_palette.js in dark mode against the
             surface (--bg of look.css, default #000000): OKLCH lightness band, chroma floor, adjacent-pair
             CVD separation (OKLab delta E x100 >= 8; 6 to 8 is a warning), adjacent-pair normal-vision
             delta E >= 15, contrast against the surface >= 3:1. A Python port with the validator's own
             constants and math; tools/test_check_look.py runs the vendored JS and asserts parity.
look.css   : only :root blocks; every token of §4 present; colour values valid #rrggbb hex;
             --ink on --bg >= 7:1 and --muted on --bg >= 4.5:1.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

DEFAULT_BG = "#000000"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

COLOR_TOKENS = ["--bg", "--panel", "--line", "--grid", "--axis", "--ink", "--ink-2", "--muted", "--accent",
                "--data", "--m-opus", "--m-sonnet", "--m-haiku", "--pos", "--neg", "--ctx-1", "--ctx-2",
                "--ok", "--warn", "--crit"]
OTHER_TOKENS = ["--f-ui", "--f-mono", "--r-1", "--r-2", "--r-3", "--gap", "--pad"]
REQUIRED_TOKENS = COLOR_TOKENS + OTHER_TOKENS

# Ported from tools/vendor/validate_palette.js (same constants, same math). Keep in lockstep.
BAND_DARK = (0.48, 0.67)       # OKLCH L
CHROMA_FLOOR = 0.10            # OKLCH C
CVD_TARGET, CVD_FLOOR = 8.0, 6.0   # OKLab delta E x100, min(protan, deutan), adjacent pairs
NORMAL_FLOOR = 15.0            # OKLab delta E x100, adjacent pairs, unsimulated
CONTRAST_MIN = 3.0             # WCAG vs surface

# Machado, Oliveira, Fernandes 2009, severity 1.0, applied to LINEAR sRGB.
MACHADO = {
    "protan": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
    "deutan": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
    "tritan": [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]],
}


# --- colour maths ------------------------------------------------------------------------------------
def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def to_linear(rgb) -> list[float]:
    return [_lin(c) for c in rgb]


def luminance(hex_color: str) -> float:
    r, g, b = to_linear(hex_to_rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def oklab_from_linear(lin: list[float]) -> tuple[float, float, float]:
    r, g, b = lin
    l = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def oklch(hex_color: str) -> tuple[float, float]:
    L, a, b = oklab_from_linear(to_linear(hex_to_rgb(hex_color)))
    return L, math.hypot(a, b)


def simulate(lin: list[float], kind: str) -> list[float]:
    m = MACHADO[kind]
    return [min(1.0, max(0.0, sum(m[i][j] * lin[j] for j in range(3)))) for i in range(3)]


def delta_e(hex_a: str, hex_b: str, kind: str | None = None) -> float:
    """OKLab distance x100; `kind` None = unsimulated vision."""
    la, lb = to_linear(hex_to_rgb(hex_a)), to_linear(hex_to_rgb(hex_b))
    if kind:
        la, lb = simulate(la, kind), simulate(lb, kind)
    a, b = oklab_from_linear(la), oklab_from_linear(lb)
    return 100 * math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])


def validate_palette(palette: list[str], surface: str = DEFAULT_BG) -> dict[str, str]:
    """The validator's checks for a dark categorical palette, adjacent pairs.
    Returns {check: 'pass' | 'floor' | 'fail' | 'relief'} (same states as the JS)."""
    lo, hi = BAND_DARK
    out = {}
    out["lightness"] = "fail" if any(not lo <= oklch(c)[0] <= hi for c in palette) else "pass"
    out["chroma"] = "fail" if any(oklch(c)[1] < CHROMA_FLOOR for c in palette) else "pass"
    pairs = [(palette[i], palette[i + 1]) for i in range(len(palette) - 1)]
    wd = min((delta_e(a, b, k) for k in ("protan", "deutan") for a, b in pairs), default=99.0)
    out["cvd"] = "pass" if wd >= CVD_TARGET else "floor" if wd >= CVD_FLOOR else "fail"
    nd = min((delta_e(a, b) for a, b in pairs), default=99.0)
    out["normal"] = "pass" if nd >= NORMAL_FLOOR else "fail"
    out["contrast"] = "relief" if any(contrast(c, surface) < CONTRAST_MIN for c in palette) else "pass"
    return out


# --- areas.yaml --------------------------------------------------------------------------------------
def check_areas(path: Path, bg: str = DEFAULT_BG) -> list[str]:
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"{path.name}: does not parse ({type(exc).__name__})"]
    if not isinstance(data, dict) or not isinstance(data.get("areas"), list) or not data["areas"]:
        return [f"{path.name}: needs a non-empty 'areas' list"]
    problems: list[str] = []
    seen: set[str] = set()
    good: list[tuple[str, str]] = []
    for i, a in enumerate(data["areas"]):
        if not isinstance(a, dict):
            problems.append(f"area #{i + 1}: not a mapping")
            continue
        aid = a.get("id")
        if not isinstance(aid, str) or not ID_RE.match(aid):
            problems.append(f"area #{i + 1}: id {aid!r} must match ^[a-z][a-z0-9-]{{0,15}}$")
            continue
        if aid in seen:
            problems.append(f"area {aid}: duplicate id")
        seen.add(aid)
        if not isinstance(a.get("name"), str) or not a["name"].strip():
            problems.append(f"area {aid}: name is empty")
        col = a.get("color")
        if not isinstance(col, str) or not HEX_RE.match(col):
            problems.append(f"area {aid}: color {col!r} must be #rrggbb")
            continue
        if contrast(col, bg) < CONTRAST_MIN:
            problems.append(f"area {aid}: color {col} has contrast {contrast(col, bg):.2f}:1 on {bg}, needs {CONTRAST_MIN:g}:1")
        good.append((aid, col))
    palette = [c for _, c in good]
    names = [i for i, _ in good]
    res = validate_palette(palette, bg)
    if res["lightness"] == "fail":
        problems.append("areas: a colour is outside the dark-mode lightness band (OKLCH L 0.48 to 0.67)")
    if res["chroma"] == "fail":
        problems.append(f"areas: a colour is below the chroma floor ({CHROMA_FLOOR}) and reads as gray")
    if res["cvd"] == "fail":
        worst = min(((delta_e(palette[i], palette[i + 1], k), names[i], names[i + 1], k) for k in ("protan", "deutan") for i in range(len(palette) - 1)))
        problems.append(f"areas {worst[1]} and {worst[2]}: adjacent pair too close for colour-blind readers ({worst[0]:.1f} < {CVD_FLOOR:g}, {worst[3]})")
    if res["normal"] == "fail":
        worst = min(((delta_e(palette[i], palette[i + 1]), names[i], names[i + 1]) for i in range(len(palette) - 1)))
        problems.append(f"areas {worst[1]} and {worst[2]}: adjacent pair too close in normal vision ({worst[0]:.1f} < {NORMAL_FLOOR:g})")
    return problems


# --- look.css ----------------------------------------------------------------------------------------
def parse_css_tokens(text: str) -> tuple[dict[str, str], list[str]]:
    """({token: value} from the :root blocks, problems about any other selector)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    tokens: dict[str, str] = {}
    problems: list[str] = []
    pos = 0
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", text):
        sel, body = m.group(1).strip(), m.group(2)
        if sel != ":root":
            problems.append(f"selector {sel[:40]!r}: look.css may only contain :root")
            continue
        for decl in body.split(";"):
            if ":" not in decl:
                continue
            k, v = decl.split(":", 1)
            k, v = k.strip(), v.strip()
            if k.startswith("--"):
                tokens[k] = v
            elif k == "color-scheme":
                tokens["color-scheme"] = v
    return tokens, problems


def check_look(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return [f"{path.name}: unreadable ({type(exc).__name__})"]
    tokens, problems = parse_css_tokens(text)
    for t in REQUIRED_TOKENS:
        if t not in tokens:
            problems.append(f"missing token {t}")
    if tokens.get("color-scheme", "").strip() != "dark":
        problems.append("color-scheme: dark is missing")
    for t in COLOR_TOKENS:
        v = tokens.get(t)
        if v is not None and not HEX_RE.match(v):
            problems.append(f"{t}: {v!r} is not a #rrggbb hex")
    bg, ink, muted = tokens.get("--bg"), tokens.get("--ink"), tokens.get("--muted")
    if bg and HEX_RE.match(bg):
        if ink and HEX_RE.match(ink) and contrast(ink, bg) < 7:
            problems.append(f"--ink on --bg is {contrast(ink, bg):.2f}:1, needs 7:1")
        if muted and HEX_RE.match(muted) and contrast(muted, bg) < 4.5:
            problems.append(f"--muted on --bg is {contrast(muted, bg):.2f}:1, needs 4.5:1")
    return problems


# --- cli ---------------------------------------------------------------------------------------------
def check_file(path: Path) -> list[str]:
    if not path.is_file():
        return [f"{path}: file not found"]
    suf = path.suffix.lower()
    if suf in (".yaml", ".yml"):
        return check_areas(path)
    if suf == ".css":
        return check_look(path)
    return [f"{path.name}: not an areas.yaml or look.css"]


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    files = [a for a in argv if a != "--json"]
    problems: list[str] = []
    if not files:
        problems.append("usage: check_look.py [--json] <areas.yaml|look.css> ...")
    bg = DEFAULT_BG
    for f in files:                      # the surface is look.css --bg when one is checked alongside
        if f.lower().endswith(".css") and Path(f).is_file():
            v = parse_css_tokens(Path(f).read_text(encoding="utf-8"))[0].get("--bg", "")
            bg = v if HEX_RE.match(v) else bg
    for f in files:
        p = Path(f)
        problems.extend(check_areas(p, bg) if p.suffix.lower() in (".yaml", ".yml") and p.is_file() else check_file(p))
    ok = not problems
    if as_json:
        print(json.dumps({"ok": ok, "problems": problems}))
    elif ok:
        print("look check: ok")
    else:
        print("look check: FAILED")
        for p in problems:
            print("  - " + p)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
