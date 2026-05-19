"""Target-weight operator command validation helpers."""

from __future__ import annotations

import shlex
from typing import Any


def command_scope_issues(
    payload: dict[str, Any],
    command: str,
    *,
    require_trade_day: bool,
    required_flags: tuple[str, ...] = (),
) -> list[str]:
    """Return operator command scope issues for a target-weight artifact."""
    candidate_id = str(payload.get("candidate_id") or "").strip()
    trade_day = str(payload.get("trade_day") or "").strip()
    issues: list[str] = []
    if not command.strip() or command.lstrip().startswith("# blocked:"):
        issues.append(command.strip() or "missing command")
        return issues
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return [f"command parse failed: {exc}"]

    def flag_value_matches(flag: str, expected: str) -> bool:
        for idx, token in enumerate(tokens):
            if token == flag and idx + 1 < len(tokens) and tokens[idx + 1] == expected:
                return True
            if token.startswith(f"{flag}=") and token.split("=", 1)[1] == expected:
                return True
        return False

    if candidate_id and not (
        flag_value_matches("--candidate-id", candidate_id)
        or flag_value_matches("--strategy", candidate_id)
    ):
        issues.append(f"candidate_id mismatch expected={candidate_id}")
    if require_trade_day and trade_day and not flag_value_matches("--as-of-date", trade_day):
        issues.append(f"as_of_date mismatch expected={trade_day}")
    for flag in required_flags:
        if flag not in tokens:
            issues.append(f"missing {flag}")
    return issues
