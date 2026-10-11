"""Fixtures: every test runs on temp copies (contracts.md section 10). Nothing here touches
D:\\AI\\atlas-data, ~/.claude, D:\\AI\\Atlas, or the repo's own schedule.yaml / tasks.yaml."""
import datetime as dt
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "cockpit") not in sys.path:
    sys.path.insert(0, str(REPO / "cockpit"))

AREAS_FIXTURE = REPO / "cockpit" / "ledger2" / "fixtures" / "areas.yaml"   # test-owned, not the live areas.yaml
SCHEDULE_FIXTURE = REPO / "cockpit" / "plan" / "fixtures" / "schedule.yaml"   # synthetic schedule fixture
SKELETON = (REPO / "cockpit" / "plan" / "fixtures" / "tasks.yaml").read_text(encoding="utf-8")   # test-owned, not the live tasks.yaml (it carries real review marks)


@pytest.fixture
def env(tmp_path, monkeypatch):
    sched = tmp_path / "schedule.yaml"
    shutil.copyfile(SCHEDULE_FIXTURE, sched)   # tests must not depend on the live calendar, which the operator re-plans
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(SKELETON, encoding="utf-8")
    areas = tmp_path / "areas.yaml"
    shutil.copyfile(AREAS_FIXTURE, areas)
    plan_dir = tmp_path / "proj"
    plan_dir.mkdir()
    (plan_dir / "plan.yaml").write_text(
        "project: learning-app\ntasks:\n"
        "  - {id: week-view, title: Week view, type: dispatch, state: next, estimate: 2h}\n"
        "  - {id: zones, title: Zones from a test, type: manual, state: todo, estimate: 90m}\n"
        "  - {id: old, title: Old thing, type: dispatch, state: done, estimate: 1h}\n", encoding="utf-8")
    reg = tmp_path / "registry.yaml"
    reg.write_text(f"projects:\n  - id: learning-app\n    plan_file: '{plan_dir / 'plan.yaml'}'\n"
                   "  - id: not-in-an-area\n    plan_file: '/nonexistent/plan.yaml'\n", encoding="utf-8")
    ver = tmp_path / "data" / "plan-version.json"
    monkeypatch.setenv("ATLAS_SCHEDULE_FILE", str(sched))
    monkeypatch.setenv("ATLAS_TASKS_FILE", str(tasks))
    monkeypatch.setenv("ATLAS_PLAN_VERSION_FILE", str(ver))
    monkeypatch.setenv("ATLAS_AREAS_FILE", str(areas))
    monkeypatch.setenv("ATLAS_REGISTRY_FILE", str(reg))
    from plan import model
    monkeypatch.setattr(model, "_auto_runs", lambda a, b: [])
    model._plan_cache.clear()
    model._reg_cache.clear()
    return type("Env", (), {"sched": sched, "tasks": tasks, "ver": ver, "tmp": tmp_path, "areas": areas})


def today_iso(offset=0):
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


@pytest.fixture
def api(env):
    from plan import api as plan_api

    class Client:
        def get(self, **q):
            code, out = plan_api.get("/api/v2/plan", {k: str(v) for k, v in q.items()})
            return code, out

        def post(self, route, body):
            return plan_api.post("/api/v2/plan/" + route, body)

        def version(self):
            return self.get()[1]["version"]

    return Client()
