"""
Deprecated paper evidence collector.

The v1 collector schema was retired. Runtime code should call
``core.paper_evidence.collect_daily_evidence`` directly so evidence records
share the v2 canonical contract used by promotion, runtime, and launch gates.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from core.paper_evidence import DailyEvidence, EVIDENCE_DIR


def collect_daily_evidence(*_args: Any, **_kwargs: Any) -> DailyEvidence | None:
    """Legacy no-op kept only so old imports fail closed instead of crashing."""
    logger.warning(
        "core.evidence_collector is deprecated; use core.paper_evidence.collect_daily_evidence"
    )
    return None
