"""The host command (docs/specs/packaging.md, step 3) against real Docker, as a person runs it: pair a runner from the
images and get a review done; update it; watch a broken update put the old runner back; uninstall. Its names carry a
test prefix (DOPPEL_HOST_PREFIX), so a real install on this machine is never touched. Doppelwerk and the model are the
fixture container from test_runner_container.py, on the runner's network.
"""

import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.test_runner_container import DATA, RUNNER, SANDBOX, SEED, docker, images, origin  # noqa: F401
from tests.integration.test_runtime_docker import REPO, VERDICT, until

pytestmark = pytest.mark.skipif(
    os.environ.get("DOPPEL_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="opt-in: set DOPPEL_DOCKER_TESTS=1 (needs Docker)",
)

ROOT = Path(__file__).resolve().parents[2]
GO = "golang:1.24.6-bookworm"


@pytest.fixture(scope="module")
def host(tmp_path_factory) -> Path:
    """The host command, built for this machine in Go's image (nothing to install here)."""
    out = tmp_path_factory.mktemp("host")
    goarch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}[platform.machine()]
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT / 'cmd' / 'doppel-runtime-host'}:/src", "-v", f"{out}:/out",
         "-w", "/src", "-e", "CGO_ENABLED=0", "-e", f"GOOS={platform.system().lower()}", "-e", f"GOARCH={goarch}",
         GO, "go", "build", "-o", "/out/doppel-runtime", "."],
        check=True, capture_output=True, timeout=900,
    )  # fmt: skip
    return out / "doppel-runtime"


async def test_pair_review_update_roll_back_and_uninstall(tmp_path, images, origin, host):  # noqa: F811
    repos, head = origin
    ids = {k: str(uuid.uuid4()) for k in ("organization", "runtime", "user", "job_id", "seat_id")}
    prefix = f"dh-test-{secrets.token_hex(3)}"
    volume, fixture = f"{prefix}-data", f"{prefix}-doppelwerk"
    owner = hashlib.sha256(f"jobs:{DATA}@{volume}".encode()).hexdigest()[:12]
    (tmp_path / "gitconfig").write_text("[safe]\n\tdirectory = *\n")
    env = {
        **os.environ,
        "DOPPEL_HOST_PREFIX": prefix,
        "DOPPEL_RUNTIME_IMAGE": RUNNER,
        "DOPPEL_SANDBOX_IMAGE": SANDBOX,
        "DOPPEL_HOST_PROVE_WITHIN": "45s",
        "DOPPEL_HOST_EXTRA_RUN_ARGS": json.dumps(
            [
                "-v",
                f"{repos}:/repos:ro",
                "-e",
                "DOPPEL_REPO_URL=/repos/{repo}",
                "-v",
                f"{tmp_path / 'gitconfig'}:/etc/doppel/gitconfig:ro",
                "-e",
                "GIT_CONFIG_GLOBAL=/etc/doppel/gitconfig",
                "-e",
                f"DOPPEL_CHATGPT_BASE=http://{fixture}:8080/v1",
            ]
        ),  # fmt: skip
    }

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(host), *args], env=env, capture_output=True, text=True, timeout=600)

    def seen() -> dict:
        return json.loads(docker("exec", fixture, "cat", "/tmp/fixture.json", check=False) or "{}")

    def runner_id() -> str:
        return docker("inspect", "--format", "{{.Id}}", prefix, check=False)

    # Doppelwerk and the model on the runner's network, and the seat's sign-in in its volume, made first; the host
    # command uses what's there.
    docker("network", "create", prefix)
    docker("volume", "create", volume)
    docker("tag", RUNNER, f"{RUNNER}-2")
    docker("build", "-q", "-t", f"{RUNNER}-broken", "-", input=f'FROM {RUNNER}\nCMD ["false"]\n')
    try:
        setup = json.dumps({**ids, "head": head, "repo": REPO, "verdict": VERDICT})
        docker("run", "-d", "--name", fixture, "--network", prefix,
               "-v", f"{Path(__file__).parent / 'doppelwerk_fixture.py'}:/fixture.py:ro",
               RUNNER, "python", "/fixture.py", setup)  # fmt: skip
        docker("run", "--rm", "-v", f"{volume}:{DATA}", RUNNER, "python", "-c", SEED, DATA,
               ids["organization"], ids["user"], datetime.now(UTC).isoformat())  # fmt: skip

        # pair: doctor, then the runner from its images, then pairing with the code on stdin.
        paired = run("pair", "dwp_container_fixture", "--bff", f"http://{fixture}:8080")
        assert paired.returncode == 0, paired.stdout + paired.stderr
        assert '"Container runner" in "CI workspace". Online.' in paired.stdout
        assert "✓ Docker" in paired.stdout, "pair runs doctor first"
        assert "dwp_container_fixture" not in docker("inspect", prefix), "the code never reaches the container's config"
        await until(lambda: seen().get("result") is not None, 300)
        assert seen()["result"]["status"] == "done", seen()["result"]

        status = run("status")
        assert (
            status.returncode == 0
            and f"runner container {prefix}" in status.stdout
            and "Container runner" in status.stdout
        )

        # update to another image: drained, swapped on the same volume, proven by a check-in, the old one removed.
        first = runner_id()
        updated = run("update", "--runner-image", f"{RUNNER}-2", "--sandbox-image", SANDBOX)
        assert updated.returncode == 0, updated.stdout + updated.stderr
        assert "Updated to dev" in updated.stdout
        second = runner_id()
        assert second and second != first
        assert docker("inspect", "--format", "{{.Config.Image}}", prefix) == f"{RUNNER}-2"
        assert not docker("ps", "-aq", "--filter", f"name=^{prefix}-previous$"), "the old container is gone"
        assert "Container runner" in run("status").stdout, "still paired: the pairing is in the volume"

        # A broken update: the new runner never checks in, and the previous one is put back as it was.
        broken = run("update", "--runner-image", f"{RUNNER}-broken", "--sandbox-image", SANDBOX)
        assert broken.returncode == 1, broken.stdout + broken.stderr
        assert "The previous version is running again" in broken.stderr
        assert runner_id() == second
        await until(lambda: "Container runner" in run("status").stdout, 60)

        # uninstall: seats signed out, the runner, its network and (with --yes) its volume removed.
        docker("rm", "-f", fixture)  # its network has to be free for the runner's to go
        gone = run("uninstall", "--yes")
        assert gone.returncode == 0, gone.stdout + gone.stderr
        assert 'Remove "Container runner" in Doppelwerk too' in gone.stdout
        assert not docker("ps", "-aq", "--filter", f"name=^{prefix}$")
        assert not docker("volume", "ls", "-q", "--filter", f"name=^{volume}$")
        assert not docker("network", "ls", "-q", "--filter", f"name=^{prefix}$")
    finally:
        docker("rm", "-f", fixture, prefix, f"{prefix}-previous", check=False)
        leftovers = docker("ps", "-aq", "--filter", f"label=doppel.owner={owner}", check=False).split()
        if leftovers:
            docker("rm", "-f", *leftovers, check=False)
        for network in docker("network", "ls", "-q", "--filter", f"label=doppel.owner={owner}", check=False).split():
            docker("network", "rm", network, check=False)
        docker("network", "rm", prefix, check=False)
        docker("volume", "rm", volume, check=False)
        docker("image", "rm", f"{RUNNER}-2", f"{RUNNER}-broken", check=False)
