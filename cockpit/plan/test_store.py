import pytest

from plan import store, versions
from plan.store import PlanError


def test_unquoted_times_are_normalised():
    data = store.parse("events:\n- id: a\n  day: 2026-10-09\n  start: 10:00\n  end: 13:30\n"
                       "  overrides:\n    2026-10-10:\n      start: 07:00\n      end: '08:00'\n", "x")
    ev = data["events"][0]
    assert (ev["start"], ev["end"], ev["day"]) == ("10:00", "13:30", "2026-10-09")
    assert ev["overrides"]["2026-10-10"] == {"start": "07:00", "end": "08:00"}


def test_parse_failure_and_non_mapping_raise_500():
    with pytest.raises(PlanError) as e:
        store.parse("events: [unclosed\n  - x: : :\n", "schedule.yaml")
    assert e.value.code == 500
    with pytest.raises(PlanError) as e:
        store.parse("- just\n- a list\n", "schedule.yaml")
    assert e.value.code == 500
    assert store.parse(None, "x") == {} and store.parse("  \n", "x") == {}


def test_section_region_backs_up_over_comments_and_blank_lines():
    text = "# head\na:\n- 1\n- 2\n\n# about b\nb:\n- x\n\n# trailing note\n"
    s, e = store.section_region(text, "a")
    assert text[s:e] == "a:\n- 1\n- 2\n"
    s, e = store.section_region(text, "b")
    assert text[s:e] == "b:\n- x\n"
    assert store.section_region(text, "zzz") == (None, None)
    s, e = store.section_region("a: []\nb: 1\n", "a")
    assert (s, e) == (0, 6)


def test_splice_keeps_everything_else_byte_identical():
    text = "# head\n\na:\n- 1\n\n# about b\nb:\n  k: v\n\n# tail\n"
    out = store.splice(text, {"a": [1, 2, 3]})
    s, e = store.section_region(text, "a")
    assert out.startswith(text[:s]) and out.endswith(text[e:])
    assert "a:\n- 1\n- 2\n- 3\n" in out
    out2 = store.splice("x: 1\n", {"events": []})
    assert out2 == "x: 1\n\nevents: []\n"


def test_splice_respects_crlf():
    text = "a:\r\n- 1\r\n\r\nb: 2\r\n"
    out = store.splice(text, {"a": [1, 2]})
    assert out == "a:\r\n- 1\r\n- 2\r\n\r\nb: 2\r\n"


def test_atomic_write_and_lock(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("a: 1\n")
    with store.locked(p):
        store.atomic_write(p, b"a: 2\n")
    assert p.read_text() == "a: 2\n"
    assert not (tmp_path / "f.yaml.tmp").exists()


def test_os_lock_busy_gives_503(tmp_path, monkeypatch):
    import threading
    p = tmp_path / "f.yaml"
    p.write_text("a: 1\n")
    monkeypatch.setattr(store, "_LOCK_TRIES", 2)
    monkeypatch.setattr(store, "_LOCK_WAIT", 0.01)
    errs = []
    held, release = threading.Event(), threading.Event()

    def holder():
        with store.locked(p):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    # a second PROCESS would hit the OS lock; in-process the RLock serialises, so test the OS lock
    # directly through a second handle
    import msvcrt
    fh = open(str(p) + ".lock", "a+b")
    fh.seek(0)
    with pytest.raises(OSError):
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    fh.close()
    release.set()
    t.join()
    assert not errs


def test_versions_detect_and_check(env):
    st = versions.load()
    assert st.fresh and st.version == 1
    assert versions.detect(st, ("evt:",), {"evt:a": {"x": 1}})
    assert st.version == 1 and st.objects["evt:a"]["v"] == 1       # baseline: no bump
    assert not versions.detect(st, ("evt:",), {"evt:a": {"x": 1}})
    assert versions.detect(st, ("evt:",), {"evt:a": {"x": 2}})
    assert st.version == 2 and st.objects["evt:a"]["v"] == 2       # outside edit: bump
    with pytest.raises(PlanError) as e:
        versions.check(st, ["evt:a"], 1, {"evt:a": {"x": 2}})
    assert e.value.code == 409 and e.value.extra["current"] == {"x": 2}
    versions.check(st, ["evt:a", "evt:other"], 2, {})
    with pytest.raises(PlanError) as e:
        versions.base_of({})
    assert e.value.code == 400
    # a vanished object is dropped and bumps
    assert versions.detect(st, ("evt:",), {})
    assert "evt:a" not in st.objects and st.version == 3
    versions.save(st)
    again = versions.load()
    assert again.version == 3 and not again.fresh
