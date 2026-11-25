"""The runner in a container (docs/specs/packaging.md, step 1): the runner image, run as the host command runs it, with
its data on a Docker volume, its own network and the host's Docker socket, pairs with a (fake) Doppelwerk and reviews a
job in a real pod. The pod's /work and /job are subpaths of the runner's volume, and its calls reach the gateway through
the forwarder on the runner's network. Doppelwerk and the model are a fixture container on the same network
(doppelwerk_fixture.py), so nothing listens on this machine; the seat's sign-in is seeded into the volume, since a
device approval can't be patched inside a container.
"""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.test_runtime_docker import REPO, VERDICT, until

pytestmark = pytest.mark.skipif(
    os.environ.get("DOPPEL_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="opt-in: set DOPPEL_DOCKER_TESTS=1 (needs Docker)",
)

ROOT = Path(__file__).resolve().parents[2]
RUNNER, SANDBOX = "doppel-runtime:test", "doppel-sandbox:test"
DATA = "/var/lib/doppel-runtime"


def docker(*args: str, check: bool = True, input: str | None = None) -> str:
    run = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=900, input=input)
    if check and run.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {run.stderr.strip()[-800:]}")
    return run.stdout.strip()


@pytest.fixture(scope="module")
def images():
    for dockerfile, tag in (("src/sandbox-runtime/Dockerfile", SANDBOX), ("src/control-plane/Dockerfile", RUNNER)):
        subprocess.run(["docker", "build", "-q", "-f", dockerfile, "-t", tag, "."],
                       cwd=ROOT, check=True, capture_output=True, timeout=1800)  # fmt: skip


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> tuple[Path, str]:
    r = tmp_path / "repos" / REPO
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "pr")
    (r / "app.py").write_text("def add(a, b):\n    return a + b  # checked\n")
    git(r, "commit", "-qam", "head")
    head = git(r, "rev-parse", "HEAD")
    git(r, "update-ref", "refs/pull/1/head", head)
    git(r, "checkout", "-q", "main")
    return tmp_path / "repos", head


HEALTHY = "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8702/healthz').status == 200 and 'ok')"
SEED = """
import json, os, sys
home = os.path.join(sys.argv[1], "tenants", sys.argv[2], sys.argv[3], "codex")
os.makedirs(home, mode=0o700)
for d in (os.path.join(sys.argv[1], "tenants"), os.path.dirname(os.path.dirname(home)), os.path.dirname(home), home):
    os.chmod(d, 0o700)
fd = os.open(os.path.join(home, "auth.json"), os.O_WRONLY | os.O_CREAT, 0o600)
os.write(fd, json.dumps({"tokens": {"access_token": "runner-ci-fixture"}, "last_refresh": sys.argv[4]}).encode())
"""


async def test_a_runner_in_a_container_pairs_and_reviews_from_its_volume(tmp_path, images, origin):
    repos, head = origin
    ids = {k: str(uuid.uuid4()) for k in ("organization", "runtime", "user", "job_id", "seat_id")}
    setup = {**ids, "head": head, "repo": REPO, "verdict": VERDICT}
    name = f"doppel-runtime-test-{secrets.token_hex(3)}"  # never a real install's names
    fixture = f"{name}-doppelwerk"
    owner = hashlib.sha256(f"jobs:{DATA}@{name}".encode()).hexdigest()[:12]
    (tmp_path / "runner.toml").write_text('[github]\nrepo_url = "/repos/{repo}"\n')
    config = ["--config", "/etc/doppel/runner.toml"]

    def seen() -> dict:
        return json.loads(docker("exec", fixture, "cat", "/tmp/fixture.json", check=False) or "{}")

    docker("network", "create", name)
    docker("volume", "create", name)
    try:
        # Doppelwerk and the model, on the runner's network only.
        docker("run", "-d", "--name", fixture, "--network", name,
               "-v", f"{Path(__file__).parent / 'doppelwerk_fixture.py'}:/fixture.py:ro",
               RUNNER, "python", "/fixture.py", json.dumps(setup))  # fmt: skip
        # The seat's Codex sign-in, as a device approval would have left it, in the volume.
        docker("run", "--rm", "-v", f"{name}:{DATA}", RUNNER, "python", "-c", SEED, DATA,
               ids["organization"], ids["user"], datetime.now(UTC).isoformat())  # fmt: skip
        docker(
            "run", "-d", "--name", name, "--network", name,
            "-v", f"{name}:{DATA}", "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{repos}:/repos:ro", "-v", f"{tmp_path / 'runner.toml'}:/etc/doppel/runner.toml:ro",
            "-e", f"DOPPEL_SANDBOX_DATA_VOLUME={name}", "-e", f"DOPPEL_SANDBOX_NETWORK={name}",
            "-e", f"DOPPEL_SANDBOX_GATEWAY_TARGET={name}:8700", "-e", f"DOPPEL_SANDBOX_IMAGE={SANDBOX}",
            "-e", f"DOPPEL_CHATGPT_BASE=http://{fixture}:8080/v1",
            # The fixture repos belong to this machine's user; git refuses another owner's repo without this.
            "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*",
            RUNNER, "doppel-runtime", "serve", *config,
        )  # fmt: skip
        await until(lambda: "ok" in docker("exec", name, "python", "-c", HEALTHY, check=False), 60)
        paired = docker("exec", name, "doppel-runtime", "pair", "dwp_container_fixture",
                        "--bff", f"http://{fixture}:8080", *config)  # fmt: skip
        assert "Paired" in paired
        await until(lambda: seen().get("result") is not None, 300)
        result = seen()["result"]
        assert result["status"] == "done", (result, docker("logs", "--tail", "60", name, check=False))
        assert result["verdict"]["verdict"] == "clean"
        assert result["verdict"]["head_sha"] == head
        assert seen()["authorization"] == ["Bearer runner-ci-fixture"]

        status = docker("exec", name, "doppel-runtime", "status", *config)
        assert "'Container runner' in 'CI workspace'" in status and "reviews run:" in status
        # Where Docker has gVisor (CI installs it, DOPPEL_TEST_SANDBOX_RUNTIME=runsc), "auto" finds and uses it.
        gvisor = os.environ.get("DOPPEL_TEST_SANDBOX_RUNTIME") == "runsc"
        assert ("under gVisor" in status) == gvisor, status
        # The pod is gone; only the forwarder is left of this runner's, on the runner's network alone.
        await until(lambda: len(docker("ps", "-q", "--filter", f"label=doppel.owner={owner}").split()) == 1, 60)
        assert docker("ps", "--filter", f"label=doppel.owner={owner}", "--format", "{{.Networks}}") == name
    finally:
        docker("rm", "-f", name, fixture, check=False)
        leftovers = docker("ps", "-aq", "--filter", f"label=doppel.owner={owner}", check=False).split()
        if leftovers:
            docker("rm", "-f", *leftovers, check=False)
        for network in docker("network", "ls", "-q", "--filter", f"label=doppel.owner={owner}", check=False).split():
            docker("network", "rm", network, check=False)
        docker("network", "rm", name, check=False)
        docker("volume", "rm", name, check=False)
