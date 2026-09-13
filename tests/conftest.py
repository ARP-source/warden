"""Shared fixtures. Every test runs against a throwaway SQLite ledger."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warden.budget import BudgetGovernor  # noqa: E402
from warden.config import BudgetConfig, load_config  # noqa: E402
from warden.ledger import Ledger  # noqa: E402
from warden.versioning import VersionStore  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    """Config pointed entirely at a temporary directory."""
    base = load_config()
    return dataclasses.replace(
        base,
        paths={
            "ledger_db": str(tmp_path / "ledger.db"),
            "ledger_jsonl": str(tmp_path / "ledger.jsonl"),
            "versions_dir": str(tmp_path / "versions"),
            "halt_file": str(tmp_path / "HALT"),
        },
    )


@pytest.fixture
def ledger(cfg):
    return Ledger(cfg, run_id="run-test")


@pytest.fixture
def governor(cfg, ledger):
    return BudgetGovernor(cfg, ledger, db_path=cfg.ledger_db)


@pytest.fixture
def tight_governor(cfg, ledger):
    """A governor with a ceiling small enough to trip inside a test."""
    tight = dataclasses.replace(
        cfg, budget=BudgetConfig(ceiling_usd=0.05, reserve_floor_usd=0.015,
                                 max_call_usd=0.02),
    )
    return BudgetGovernor(tight, ledger, db_path=cfg.ledger_db)


@pytest.fixture
def store(cfg):
    return VersionStore(cfg)
