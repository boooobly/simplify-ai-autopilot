import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_railway_python_matches_the_ci_runtime() -> None:
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.11"


def test_railway_worker_configuration_is_explicit_and_singleton_safe() -> None:
    config = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))

    assert config["$schema"] == "https://railway.com/railway.schema.json"
    assert config["build"]["builder"] == "RAILPACK"

    deploy = config["deploy"]
    assert deploy["startCommand"] == "python main.py"
    assert deploy["restartPolicyType"] == "ON_FAILURE"
    assert deploy["restartPolicyMaxRetries"] >= 10
    assert deploy["overlapSeconds"] == "0"
    assert int(deploy["drainingSeconds"]) >= 10
