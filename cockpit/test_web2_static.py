"""cockpit/web2_static.py: traversal safety, 404 once index exists, cache-busting, ETag/304
(docs/v2/contracts.md §5 intro, §8.1).

Run:  py -3.12 -m pytest -q cockpit/test_web2_static.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import web2_static  # noqa: E402

INDEX = """<!doctype html><html><head><title>t</title>
<link href="https://fonts.googleapis.com/css2?family=Geist" rel="stylesheet">
<link rel="stylesheet" href="/look.css"><link rel="stylesheet" href="screens/x.css">
<link rel="icon" href="data:,">
</head><body><a href="#/plan">p</a><script type="module" src="/app.js"></script></body></html>"""


@pytest.fixture()
def site(tmp_path, monkeypatch):
    root = tmp_path / "web2"
    (root / "kit").mkdir(parents=True)
    (root / "screens").mkdir()
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "look.css").write_text(":root{--bg:#000000}", encoding="utf-8")
    (root / "screens" / "x.css").write_text(".x{}", encoding="utf-8")
    (root / "app.js").write_text("import './kit/a.js';", encoding="utf-8")
    (root / "kit" / "a.js").write_text("export const a=1;", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WEB2_DIR", str(root))
    return root


def sha10(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()[:10]


def test_none_while_index_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WEB2_DIR", str(tmp_path / "empty"))
    assert web2_static.serve("/") is None
    assert web2_static.serve("/anything.js") is None


def test_missing_file_is_404_once_index_exists(site):
    code, ctype, body, headers = web2_static.serve("/nope.js")
    assert code == 404 and body == b"not found"


@pytest.mark.parametrize("route", ["/../secret.txt", "/..%2Fsecret.txt", "/%2e%2e/secret.txt", "/kit/../../secret.txt",
                                   "/..\\secret.txt", "/%00", "//../secret.txt"])
def test_traversal_is_404(site, route):
    res = web2_static.serve(route)
    assert res is not None and res[0] == 404 and b"secret" not in res[2]


def test_a_directory_is_404(site):
    assert web2_static.serve("/kit")[0] == 404


def test_root_serves_index_with_versioned_local_urls(site):
    code, ctype, body, headers = web2_static.serve("/")
    html = body.decode()
    assert code == 200 and ctype.startswith("text/html")
    assert f'href="/look.css?v={sha10(site / "look.css")}"' in html
    assert f'href="screens/x.css?v={sha10(site / "screens" / "x.css")}"' in html
    assert f'src="/app.js?v={sha10(site / "app.js")}"' in html
    # untouched: external, fragment, data URL
    assert 'href="https://fonts.googleapis.com/css2?family=Geist"' in html
    assert 'href="#/plan"' in html and 'href="data:,"' in html


def test_importmap_lists_every_js_and_comes_first(site):
    html = web2_static.serve("/index.html")[2].decode()
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    assert m and html.index("importmap") < html.index('type="module"')
    imports = json.loads(m.group(1))["imports"]
    assert imports == {"/app.js": f"/app.js?v={sha10(site / 'app.js')}", "/kit/a.js": f"/kit/a.js?v={sha10(site / 'kit' / 'a.js')}"}


def test_version_changes_when_a_file_changes(site):
    before = web2_static.serve("/")[2]
    (site / "kit" / "a.js").write_text("export const a=222;", encoding="utf-8")
    after = web2_static.serve("/")[2]
    assert before != after


def test_headers_etag_and_304(site):
    code, ctype, body, headers = web2_static.serve("/look.css")
    assert code == 200 and ctype.startswith("text/css") and headers["Cache-Control"] == "no-cache"
    etag = headers["ETag"]
    code2, _, body2, h2 = web2_static.serve("/look.css", etag)
    assert code2 == 304 and body2 == b"" and h2["ETag"] == etag
    assert web2_static.serve("/look.css", 'W/' + etag)[0] == 304
    assert web2_static.serve("/look.css", '"other"')[0] == 200
    # index: the etag follows the rendered page, so a changed asset invalidates it
    i_etag = web2_static.serve("/")[3]["ETag"]
    assert web2_static.serve("/", i_etag)[0] == 304
    (site / "look.css").write_text(":root{--bg:#010101}", encoding="utf-8")
    assert web2_static.serve("/", i_etag)[0] == 200


def test_content_types(site):
    assert web2_static.serve("/app.js")[1].startswith("text/javascript")
    (site / "sample").mkdir()
    (site / "sample" / "d.json").write_text("{}", encoding="utf-8")
    assert web2_static.serve("/sample/d.json")[1].startswith("application/json")


def test_the_real_site_serves_when_present():
    root = HERE / "web2"
    if not (root / "index.html").exists():
        pytest.skip("web2 not built")
    code, ctype, body, headers = web2_static.serve("/")
    assert code == 200
    imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', body.decode(), re.S).group(1))["imports"]
    assert set(imports) == {"/" + p.relative_to(root).as_posix() for p in root.rglob("*.js")}
    for rel in imports:
        assert web2_static.serve(rel)[0] == 200
    for href in re.findall(r'(?:href|src)="(/[^"?#]+)\?v=[0-9a-f]{10}"', body.decode()):
        assert web2_static.serve(href)[0] == 200, href
