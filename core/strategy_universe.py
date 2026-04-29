"""
Canonical Strategy Universe

operator-facing 전략 목록의 single source of truth.

Source priority:
  1. strategies/__init__.py STRATEGY_STATUS (code registry — hard gate)
  2. approved_strategies.json (promotion status)
  3. runtime state (paper_runtime)

규칙:
  - paper 대상 = STRATEGY_STATUS에서 allowed_modes에 "paper" 포함
  - evidence JSONL 파일 존재만으로는 전략 목록에 포함되지 않음
  - test/demo strategy (dedup_test 등)는 production view에서 제외
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger


def get_paper_strategies() -> list[dict]:
    """paper 대상 전략 목록. 각 dict에 name, registry_status, allowed_modes 포함."""
    from strategies import STRATEGY_STATUS

    results = []
    for name, info in STRATEGY_STATUS.items():
        modes = info.get("allowed_modes", [])
        if "paper" in modes:
            results.append({
                "name": name,
                "registry_status": info.get("status", "unknown"),
                "allowed_modes": modes,
            })
    return results


def get_paper_strategy_names() -> list[str]:
    """paper 대상 전략 이름만 반환."""
    return [s["name"] for s in get_paper_strategies()]


def is_paper_eligible(strategy: str) -> bool:
    """이 전략이 paper 대상인지."""
    return strategy in get_paper_strategy_names()


def get_strategy_display_name(strategy: str) -> str:
    """operator-facing 표시명. registry의 key를 그대로 사용."""
    return strategy
