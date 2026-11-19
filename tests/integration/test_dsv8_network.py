"""DSV-8 with real containers: a pod's network reaches the gateway's model route and nothing else.

Opt-in (needs Docker and pulls python:3.12-slim): DOPPEL_DOCKER_TESTS=1 uv run pytest tests/integration

Layout, as on summini: the runtime container sits on an ordinary bridge (it reaches the internet)
and on an `--internal` network as `gateway`. Its model listener binds every interface; its internal
listener binds 127.0.0.1 only. A "pod" container sits on the internal network alone.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DOPPEL_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="opt-in: set DOPPEL_DOCKER_TESTS=1 (needs Docker)",
)

REPO = Path(__file__).resolve().parents[2]
IMAGE = "python:3.12-slim"
PYTHONPATH = ":".join(f"/src/{c}" for c in ("shared", "control-plane", "gateway", "model-provider"))

PROBE = r"""
import json, socket, urllib.request, urllib.error
def get(url, method="GET"):
    try:
        req = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__
print(json.dumps({
    "model_health": get("http://gateway:8700/healthz"),
    "model_route_no_capability": get("http://gateway:8700/model/responses", "POST"),
    "internal": get("http://gateway:8701/healthz"),
    "health": get("http://gateway:8702/healthz"),
    "github": get("https://github.com"),
    "raw_ip": get("http://1.1.1.1"),
}))
"""


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=600).stdout.strip()


@pytest.fixture
def network():
    tag = uuid.uuid4().hex[:8]
    net, gw = f"doppel-dsv8-{tag}", f"doppel-gw-{tag}"
    docker("network", "create", "--internal", net)
    try:
        docker(
            "run", "-d", "--name", gw, "-v", f"{REPO}/src:/src:ro",
            "-e", f"PYTHONPATH={PYTHONPATH}", "-e", "DOPPEL_DATA_DIR=/tmp/doppel",
            "-e", "DOPPEL_MODEL_HOST=0.0.0.0", "-e", "DOPPEL_MODEL_PORT=8700",
            "-e", "DOPPEL_INTERNAL_HOST=127.0.0.1", "-e", "DOPPEL_INTERNAL_PORT=8701",
            "-e", "DOPPEL_HEALTH_HOST=127.0.0.1", "-e", "DOPPEL_HEALTH_PORT=8702",
            IMAGE, "sh", "-c", "pip install -q aiohttp && python -m doppel_control_plane serve",
        )  # fmt: skip
        docker("network", "connect", "--alias", "gateway", net, gw)
        deadline = time.monotonic() + 180
        while (
            "ready "
            not in docker("logs", gw, check=False)
            + subprocess.run(["docker", "logs", gw], capture_output=True, text=True).stderr
        ):
            if time.monotonic() > deadline:
                pytest.fail("runtime container never became ready:\n" + docker("logs", gw, check=False))
            time.sleep(1)
        yield net
    finally:
        docker("rm", "-f", gw, check=False)
        docker("network", "rm", net, check=False)


def test_dsv_8_a_pod_reaches_the_model_route_and_nothing_else(network):
    out = docker("run", "--rm", "--network", network, IMAGE, "python", "-c", PROBE)
    seen = json.loads(out.splitlines()[-1])
    print("pod saw:", seen)
    assert seen["model_health"] == 200
    assert seen["model_route_no_capability"] == 401  # the gateway's own refusal: nothing went upstream
    assert seen["internal"] != 200 and seen["health"] != 200
    assert seen["github"] != 200 and seen["raw_ip"] != 200
