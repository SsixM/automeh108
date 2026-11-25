from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def data_dir(root: Path) -> Path:
    return root / "data"


@pytest.fixture(scope="session")
def schema_dir(root: Path) -> Path:
    return root / "schemas"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES
