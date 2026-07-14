import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_fatal_startup_traceback_does_not_bypass_secret_redaction() -> None:
    secret = "123456789:TEST_FATAL_STARTUP_SECRET"
    script = """
import os
import main

def fail_with_secret():
    main.setup_logging()
    raise RuntimeError(f"startup rejected {os.environ['BOT_TOKEN']}")

main.main = fail_with_secret
main.run_main_safely()
"""
    env = os.environ.copy()
    env["BOT_TOKEN"] = secret

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert secret not in output
    assert "[REDACTED]" in output
    assert "fatal: bot startup failed" in output


def test_logging_routes_info_to_stdout_and_errors_to_stderr() -> None:
    secret = "123456789:TEST_LOG_STREAM_SECRET"
    script = """
import logging
import os
import main

main.setup_logging()
logger = logging.getLogger("stream-test")
logger.info("ordinary startup message")
logger.error("rejected credential %s", os.environ["BOT_TOKEN"])
"""
    env = os.environ.copy()
    env["BOT_TOKEN"] = secret

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "ordinary startup message" in result.stdout
    assert "ordinary startup message" not in result.stderr
    assert "rejected credential" in result.stderr
    assert "rejected credential" not in result.stdout
    assert secret not in result.stdout + result.stderr
    assert "[REDACTED]" in result.stderr
