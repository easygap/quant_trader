"""중복 YAML 키는 마지막 값으로 조용히 덮지 않고 설정 로드 단계에서 차단한다."""

import pytest
import yaml

from config.config_loader import load_yaml


def test_duplicate_yaml_key_is_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text(
        "strategy:\n  threshold: 1\nstrategy:\n  threshold: 2\n",
        encoding="utf-8",
    )

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        load_yaml(path)


def test_nested_duplicate_yaml_key_is_rejected(tmp_path):
    path = tmp_path / "nested_duplicate.yaml"
    path.write_text(
        "strategy:\n  threshold: 1\n  threshold: 2\n",
        encoding="utf-8",
    )

    with pytest.raises(yaml.constructor.ConstructorError, match="threshold"):
        load_yaml(path)


def test_project_strategy_config_has_unique_keys():
    loaded = load_yaml("config/strategies.yaml")
    assert "trend_pullback" in loaded
    assert loaded["trend_pullback"]["sma_period"] == 60
