"""The process end to end: `doppel-runtime serve` starts, answers health, and recovers from a crash."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from doppel_control_plane.config import LISTENERS


def start(data_dir: Path) -> tuple[subprocess.Popen, dict[str, int]]:
    env = {**os.environ, "DOPPEL_DATA_DIR": str(data_dir), **{f"DOPPEL_{n.upper()}_PORT": "0" for n in LISTENERS}}
    proc = subprocess.Popen(
        [sys.executable, "-m", "doppel_control_plane", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.startswith("ready "):
            ports = {kv.split("=")[0]: int(kv.rsplit(":", 1)[1]) for kv in line.split()[1:]}
            return proc, ports
        if proc.poll() is not None:
            break
    proc.kill()
    pytest.fail(f"runtime did not start: {proc.stderr.read()}")


def health(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
        assert r.status == 200
        return json.loads(r.read())


def test_serve_answers_health_on_every_listener(tmp_path):
    proc, ports = start(tmp_path)
    try:
        assert set(ports) == set(LISTENERS)
        for name, port in ports.items():
            body = health(port)
            assert body["ok"] and body["listener"] == name
            assert body["roles"] == {"broker": True, "gateway": True, "sandbox": True, "bff": True}
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(10) == 0


def test_a_job_left_running_by_a_crash_is_dropped_for_doppelwerk_to_retry(tmp_path):
    proc, _ = start(tmp_path)
    db = sqlite3.connect(tmp_path / "ledger.db", isolation_level=None)
    db.execute("INSERT INTO prs (tenant, repo, pr, current_head, created_at) VALUES ('s','o/r',1,'aaa','t')")
    db.execute(
        "INSERT INTO slots (pr_id, head_sha, reviewer, reason, command_id, state, queued_at, started_at, bff_job) "
        "VALUES (1,'aaa','steven','app','j1','running','t','t','j1')"
    )
    proc.kill()  # SIGKILL: no shutdown path runs
    proc.wait(10)
    assert db.execute("SELECT state FROM slots").fetchone()[0] == "running"

    proc, ports = start(tmp_path)
    try:
        health(ports["health"])
        # Its pod went with the process; Doppelwerk sees it missing from the next check-in and tries it again.
        assert db.execute("SELECT state FROM slots").fetchone()[0] == "failed"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
        db.close()


def test_dsv_8_sign_in_routes_are_on_the_internal_listener_only(tmp_path):
    proc, ports = start(tmp_path)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{ports['internal']}/broker/summation/u-steven/status", timeout=5
        ) as r:
            assert json.loads(r.read())["state"] == "signed_out"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{ports['model']}/broker/summation/u-steven/status", timeout=5)
        assert e.value.code == 404
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
