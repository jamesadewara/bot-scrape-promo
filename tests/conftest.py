from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    def _read(name: str) -> str:
        return (FIX / name).read_text(encoding="utf-8")
    return _read


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """Tests use built-in defaults: never the operator's .env or PROMOSCOUT_* variables."""
    import os
    for k in list(os.environ):
        if k.startswith("PROMOSCOUT_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)
