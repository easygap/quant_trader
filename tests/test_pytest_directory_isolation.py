"""Check actual child-process shutdown without touching any caller-owned path."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ISOLATED_NAMES = (
    "QUANT_RESEARCH_OPERATIONS_DIR", "QUANT_PAPER_VALIDATION_DIR", "QUANT_MARKET_INTELLIGENCE_DIR",
    "QUANT_DAILY_OBSERVATION_DIR",
    "QUANT_OVERLAY_STATE_DIR", "QUANT_DASHBOARD_STATE_PATH", "QUANT_RUNTIME_LOCK_DIR",
)


def _child(script, tmp_path):
    env = os.environ.copy()
    # Skip ORM setup entirely; this DB is neither opened nor created by the hook.
    env["QUANT_DB_PATH"] = str(tmp_path / "caller_database.db")
    result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT / "tests" / "conftest.py"), str(tmp_path)],
                            capture_output=True, text=True, check=True, env=env, timeout=20)
    return json.loads(result.stdout)


def test_supplied_paths_create_zero_directories_and_register_zero_cleanup(tmp_path):
    result = _child("""
import atexit, json, os, runpy, sys, tempfile
from pathlib import Path
module = runpy.run_path(sys.argv[1])
names = %r
expected = {name: str(Path(sys.argv[2]) / name.lower()) for name in names}
os.environ.update(expected)
def forbidden(*args, **kwargs):
    raise AssertionError('caller path must not create a directory or register deletion')
tempfile.mkdtemp = forbidden
atexit.register = forbidden
module['pytest_configure'](None)
assert all(os.environ[name] == value for name, value in expected.items())
print(json.dumps({'new_directories': 0, 'registered_cleanup': 0}))
""" % (ISOLATED_NAMES,), tmp_path)
    assert result == {"new_directories": 0, "registered_cleanup": 0}
    assert not list(tmp_path.iterdir())


def test_created_directories_removed_at_exit_even_after_env_points_to_user_directory(tmp_path):
    user_directory = tmp_path / "user-preserved"
    user_directory.mkdir()
    sentinel = user_directory / "existing.txt"
    sentinel.write_text("keep this caller file", encoding="utf-8")
    result = _child("""
import json, os, runpy, sys, tempfile
from pathlib import Path
module = runpy.run_path(sys.argv[1])
names = %r
for name in names:
    os.environ.pop(name, None)
tempfile.tempdir = sys.argv[2]
module['pytest_configure'](None)
first = {name: os.environ[name] for name in names}
module['pytest_configure'](None)
assert first == {name: os.environ[name] for name in names}
directories = [str(Path(value).parent if name.endswith('_PATH') else Path(value)) for name, value in first.items()]
assert len(set(directories)) == 7 and all(Path(path).is_dir() for path in directories)
for name in names:
    os.environ[name] = str(Path(sys.argv[2]) / 'user-preserved')
print(json.dumps({'created_directories': directories}))
""" % (ISOLATED_NAMES,), tmp_path)
    directories = [Path(value) for value in result["created_directories"]]
    assert all(path.parent == tmp_path.resolve() for path in directories)
    assert all(not path.exists() for path in directories)
    assert sentinel.read_text(encoding="utf-8") == "keep this caller file"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["user-preserved"]
