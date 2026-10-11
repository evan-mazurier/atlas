"""Price weights: fresh tokens in Sonnet-equivalent units (Sonnet 5.5 = 1.0)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import usage     # noqa: E402


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("ATLAS_MODEL_WEIGHTS", raising=False)


@pytest.mark.parametrize("model,prompt,want", [
    ("claude-haiku-5-5", 0, 0.05),
    ("claude-haiku-5-5", 100_000, 0.05),
    ("claude-haiku-5-5", 100_001, 0.25),
    ("claude-haiku-4-5-20251001", 0, 0.5),
    ("claude-haiku-4-5-20251001", 500_000, 0.5),
    ("claude-opus-5-5", 0, 2.0),
    ("claude-sonnet-5-5", 0, 1.0),
    ("<synthetic>", 0, 1.0),
    ("claude-mystery-9", 0, 1.0),
    (None, 0, 1.0),
])
def test_default_table(model, prompt, want):
    assert usage.model_weight(model, prompt) == want


def test_env_replaces_and_adds_patterns(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_WEIGHTS", "haiku-5=0.1,opus=3,mystery=7")
    assert usage.model_weight("claude-haiku-5-5", 0) == 0.1
    assert usage.model_weight("claude-haiku-5-5", 200_000) == 0.1     # flat, no tier
    assert usage.model_weight("claude-opus-5-5") == 3
    assert usage.model_weight("claude-mystery-9") == 7
    assert usage.model_weight("claude-sonnet-5-5") == 1.0             # untouched default


def test_malformed_env_is_ignored(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_WEIGHTS", "garbage,opus=,=2,sonnet=abc,haiku-5=0.07, ,opus=nan")
    assert usage.model_weight("claude-opus-5-5") == 2.0
    assert usage.model_weight("claude-sonnet-5-5") == 1.0
    assert usage.model_weight("claude-haiku-5-5") == 0.07
