"""
스케줄러 ↔ 웹 대시보드 간 런타임 스냅샷 (JSON 파일).

스케줄러가 주기적으로 갱신하고, 대시보드 프로세스가 읽는다.
동일 프로세스에서만 메모리 공유가 되므로 파일 기반으로 분리 실행을 지원한다.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

_lock = threading.Lock()
_MAX_SIGNALS = 400


def _state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "dashboard_runtime_state.json"


def _read_unlocked() -> Dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_unlocked(data: Dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def read_state() -> Dict[str, Any]:
    """대시보드용 읽기 (스냅샷 일관성은 강하게 보장하지 않음)."""
    with _lock:
        return dict(_read_unlocked())


def merge_runtime(updates: Dict[str, Any]) -> None:
    """스케줄러가 kis_stats, blackswan, loop_metrics 등을 병합 저장."""
    if not updates:
        return
    with _lock:
        cur = _read_unlocked()
        for k, v in updates.items():
            cur[k] = v
        _write_unlocked(cur)


def append_signal(
    symbol: str,
    signal: str,
    score: Any,
    *,
    strategy_name: str = "",
    source: str = "",
) -> None:
    """당일 신호 목록에 1건 추가 (날짜가 바뀌면 리스트 초기화)."""
    entry = {
        "symbol": symbol,
        "signal": signal,
        "score": float(score) if score is not None and score != "" else 0.0,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "",
    }
    with _lock:
        cur = _read_unlocked()
        today = date.today().isoformat()
        if cur.get("signals_date") != today:
            cur["signals_today"] = []
            cur["signals_date"] = today
        sigs: List[dict] = list(cur.get("signals_today") or [])
        sigs.append(entry)
        cur["signals_today"] = sigs[-_MAX_SIGNALS:]
        if strategy_name:
            cur["strategy"] = strategy_name
        _write_unlocked(cur)
