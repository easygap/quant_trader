from core.target_weight_commands import command_scope_issues


def test_command_scope_accepts_exact_candidate_and_equals_style_date():
    payload = {
        "candidate_id": "target_weight_best",
        "trade_day": "2026-05-20",
    }
    command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --as-of-date=2026-05-20 "
        "--execute --collect-evidence"
    )

    assert command_scope_issues(
        payload,
        command,
        require_trade_day=True,
        required_flags=("--execute", "--collect-evidence"),
    ) == []


def test_command_scope_rejects_candidate_prefix_collision():
    payload = {
        "candidate_id": "target_weight_best",
        "trade_day": "2026-05-20",
    }
    command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best_shadow --as-of-date 2026-05-20 "
        "--execute --collect-evidence"
    )

    issues = command_scope_issues(
        payload,
        command,
        require_trade_day=True,
        required_flags=("--execute", "--collect-evidence"),
    )

    assert issues == ["candidate_id mismatch expected=target_weight_best"]


def test_command_scope_rejects_missing_required_flags_and_bad_date():
    payload = {
        "candidate_id": "target_weight_best",
        "trade_day": "2026-05-20",
    }
    command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --as-of-date 2026-05-19 "
        "--readiness-audit"
    )

    issues = command_scope_issues(
        payload,
        command,
        require_trade_day=True,
        required_flags=("--execute", "--collect-evidence"),
    )

    assert "as_of_date mismatch expected=2026-05-20" in issues
    assert "missing --execute" in issues
    assert "missing --collect-evidence" in issues
