"""tools/find_plan_item.py on fixture data. No live server, no live files."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "cockpit"))

import find_plan_item as fpi  # noqa: E402

PAYLOAD = {
    "events": [
        {"id": "evt_aaaaaaaa", "occurrence": "2026-10-12", "title": "Product: open weekly report",
         "day": "2026-10-12", "start": "15:45", "end": "16:30", "area": "md", "recur": None},
        {"id": "evt_bbbbbbbb", "occurrence": "2026-10-12", "title": "Pull workout", "day": "2026-10-12",
         "start": "07:30", "end": "08:30", "area": "train", "recur": {"every": "week"}},
        {"id": "evt_cccccccc", "occurrence": "2026-10-13", "title": "Weekly report (other day)",
         "day": "2026-10-13", "start": "09:00", "end": "10:00", "area": "md", "recur": None},
    ],
    "tasks": {
        "md": [
            {"id": "t_11111111", "title": "Écrire le suivi des paiements fondateurs", "state": "todo", "owner": "me"},
            {"id": "t_22222222", "title": "Payment page copy", "state": "done", "owner": "me"},
            {"id": "t_33333333", "title": "Payment reminder", "state": "next", "owner": "me",
             "planned": {"day": "2026-10-20", "start": "09:00", "end": "10:00", "on": "me"}},
            {"id": "t_44444444", "title": "Payment webhook", "state": "next", "owner": "me",
             "planned": {"day": "2026-10-13", "start": "09:00", "end": "10:00", "on": "me"}},
        ],
        "atlas": [{"id": "atlas/week-view", "title": "Week view", "state": "next", "owner": "agents"}],
    },
}
DAY = "2026-10-12"


def ids(query, day=DAY):
    return [h["id"] for h in fpi.find(PAYLOAD, query, day)]


def test_fold_is_case_and_accent_insensitive():
    assert fpi.fold("  Écrire LE  Suivi! ") == "ecrire le suivi"
    assert fpi.score("ECRIRE", "Écrire") == 100


def test_exact_beats_contains_beats_word_overlap():
    assert fpi.score("pull workout", "Pull Workout") == 100
    assert fpi.score("workout", "Pull workout") == 80
    assert fpi.score("report weekly", "Product: open weekly report") == 60
    assert 0 < fpi.score("open payment email", "Product: open weekly report") < 80
    assert fpi.score("zzz", "Pull workout") == 0


def test_word_prefix_counts():
    assert fpi.score("week rep", "open weekly report") == 60


def test_finds_event_of_the_day_best_first():
    hits = fpi.find(PAYLOAD, "weekly report", DAY)
    assert hits[0]["id"] == "evt_aaaaaaaa" and hits[0]["kind"] == "event" and hits[0]["area"] == "md"
    assert hits[0]["when"] == "2026-10-12 15:45-16:30"
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert "evt_cccccccc" not in ids("weekly report")           # events: that day only


def test_series_occurrence_gets_a_dated_id():
    assert ids("pull workout") == ["evt_bbbbbbbb@2026-10-12"]


def test_tasks_open_only_and_within_one_day_or_unplanned():
    got = ids("payment")
    assert "t_22222222" not in got                                 # done
    assert "t_33333333" not in got                                 # planned 8 days away
    assert "t_44444444" in got                                     # planned the next day
    assert fpi.find(PAYLOAD, "suivi paiements", DAY)[0]["id"] == "t_11111111"
    assert fpi.find(PAYLOAD, "week view", DAY)[0]["id"] == "atlas/week-view"


def test_no_match_is_empty(capsys):
    assert ids("nothing like this") == []


def test_main_prints_one_tab_line_per_match(monkeypatch, capsys):
    monkeypatch.setattr(fpi, "fetch_live", lambda *a, **k: PAYLOAD)
    assert fpi.main(["weekly", "report", "--day", DAY]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t")[:3] == ["evt_aaaaaaaa", "80", "event"]
    assert all(len(l.split("\t")) == 6 for l in lines)


def test_main_no_match_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(fpi, "fetch_live", lambda *a, **k: PAYLOAD)
    assert fpi.main(["zzzz", "--day", DAY]) == 0
    assert capsys.readouterr().out == ""


def test_main_bad_day(capsys):
    assert fpi.main(["x", "--day", "tomorrow"]) == 2


def test_offline_reads_the_files_without_writing(monkeypatch, tmp_path):
    sched = tmp_path / "schedule.yaml"
    sched.write_text("events:\n- {id: evt_0a1b2c3d, title: 'Open weekly report', day: '2026-10-12',"
                     " start: '15:45', end: '16:30', type: errand}\n", encoding="utf-8")
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text("tasks:\n- {id: t_deadbeef, area: md, title: Write outreach, state: todo, owner: me}\n",
                     encoding="utf-8")
    areas = tmp_path / "areas.yaml"
    areas.write_text((HERE.parent / "cockpit" / "ledger2" / "fixtures" / "areas.yaml").read_text(encoding="utf-8"),
                     encoding="utf-8")
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects: []\n", encoding="utf-8")
    ver = tmp_path / "plan-version.json"
    for k, v in (("ATLAS_SCHEDULE_FILE", sched), ("ATLAS_TASKS_FILE", tasks), ("ATLAS_AREAS_FILE", areas),
                 ("ATLAS_REGISTRY_FILE", reg), ("ATLAS_PLAN_VERSION_FILE", ver)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(fpi, "fetch_live", lambda *a, **k: None)      # server down
    from plan import model
    model._reg_cache.clear()
    model._plan_cache.clear()
    p = fpi.load_offline("2026-10-11", "2026-10-13")
    got = [h["id"] for h in fpi.find(p, "weekly report", DAY)]
    assert got == ["evt_0a1b2c3d"]
    assert [h["id"] for h in fpi.find(p, "outreach", DAY)] == ["t_deadbeef"]
    assert not ver.exists()
