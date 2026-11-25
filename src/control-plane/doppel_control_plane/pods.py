"""The sandbox manager: one Docker container per PR (the `Sandbox` port), spec section 6.

    create_pod      a network of its own (`--internal`: no route out), the egress forwarder joined
                    to it as `gateway`, and the pod: read-only root, no capabilities, /work and /job
                    mounted read-only, idling as the `pod` user
    start_reviewer  check out the head (once per head), write the job, then
                    `docker exec --user <its own uid> -e AGENT_CAPABILITY pod python -m doppel_sandbox.review`
    stop_reviewer   kill that uid's processes; the pod stays
    delete_pod      the pod, its network, its checkouts and jobs

Each reviewer run gets its own Unix user from the image's pool, and its capability only in its own
process's environment, which another user can't read (review #5). The capability is passed by name
(`-e AGENT_CAPABILITY`), so it never appears on a command line.

A run's verdict (the last stdout line) goes to `on_verdict(slot_id, verdict)`; anything else to
`on_failed(slot_id, why)`. On summini the pod runs under gVisor (`runtime="runsc"`).

Where the runner itself runs in a container (docs/specs/packaging.md), its data folder is a Docker volume
(`data_volume`): a pod's /work and /job are then that volume's subpaths, since a folder path inside the runner means
nothing to the Docker host. And the egress forwarder joins the runner's own network (`network`) to reach its gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path

from doppel_shared.ports import ReviewJob
from doppel_shared.types import PRRef

from doppel_control_plane.mirrors import Mirrors

log = logging.getLogger(__name__)

UID_BASE, UID_POOL = 10000, 16  # reviewer1..reviewer16 in the image
EGRESS_PORT = 8700
# The pods' one way out: a forwarder to the gateway and nowhere else, joined to each pod's network.
EGRESS_SCRIPT = r"""
import asyncio, sys
host, port = sys.argv[1].rsplit(":", 1)
async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data); await w.drain()
    except OSError:
        pass
    finally:
        w.close()
async def handle(r, w):
    try:
        ur, uw = await asyncio.open_connection(host, int(port))
    except OSError:
        w.close(); return
    await asyncio.gather(pipe(r, uw), pipe(ur, w))
async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8700)
    await server.serve_forever()
asyncio.run(main())
"""


def write_doppel_files(root: Path, files: dict[str, str]) -> None:
    """A doppel's files, readable by the reviewer. Each path must stay inside `root`: no absolute paths, no `..`, no dot
    folders (the BFF checks the same; the pod is the boundary, but a file is never written outside the job's folder)."""
    for name, text in files.items():
        parts = name.split("/")
        if not name or name.startswith("/") or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            log.warning("skipped a doppel file with an unsafe path")
            continue
        path = root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for path in [root, *root.rglob("*")] if root.exists() else []:
        path.chmod(0o755 if path.is_dir() else 0o644)


def docker(*args: str, check: bool = True, timeout: int = 300) -> str:
    run = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and run.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {run.stderr.strip()[-500:]}")
    return run.stdout.strip()


def detect_runtime(configured: str | None) -> str | None:
    """The pods' OCI runtime: as configured, or with "auto", gVisor where Docker has `runsc` registered."""
    if configured != "auto":
        return configured
    try:
        runtimes = json.loads(docker("info", "--format", "{{json .Runtimes}}", check=False, timeout=10) or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return "runsc" if isinstance(runtimes, dict) and "runsc" in runtimes else None


def isolation(runtime: str | None) -> str:
    """How pods are isolated, as Doppelwerk shows it: gVisor, or Docker's standard runtime."""
    return "gvisor" if runtime == "runsc" else "standard"


def docker_bridge_gateway() -> str | None:
    """The local IPv4 bridge address that a Linux Docker container uses to reach its host."""
    try:
        gateways = docker(
            "network", "inspect", "bridge", "--format", "{{range .IPAM.Config}}{{println .Gateway}}{{end}}",
            check=False, timeout=5,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    for gateway in gateways.splitlines():
        with suppress(ValueError):
            return str(IPv4Address(gateway))
    return None


@dataclass
class Pod:
    id: str
    pr: PRRef
    network: str
    folder: Path
    uids: dict[int, int] = field(default_factory=dict)  # slot -> uid
    runs: dict[int, asyncio.Task] = field(default_factory=dict)


class DockerPods:
    def __init__(
        self,
        root: Path,
        mirrors: Mirrors,
        *,
        image: str,
        gateway_target: str,  # where the egress forwarder sends pods' calls, e.g. host.docker.internal:8700
        repo_url: Callable[[PRRef], str],
        on_verdict: Callable[[int, dict], None],
        on_failed: Callable[[int, str], None],
        pr_ref: Callable[[PRRef], str] = lambda pr: f"refs/pull/{pr.number}/head",
        base_branch: Callable[[PRRef], str | None] = lambda pr: None,  # the PR's base; None: the repo's default
        runtime: str | None = None,  # "runsc" for gVisor
        egress_image: str = "python:3.12-slim-bookworm",
        memory: str = "2g",
        cpus: str = "1",
        owner: str = "",  # labels this runtime's containers and networks, so `sweep` removes only its own
        data_volume: str | None = None,  # the runner's data folder is this volume, mounted at `data_root`
        data_root: Path | None = None,
        network: str | None = None,  # the runner's own network: the forwarder joins it to reach `gateway_target`
    ):
        if data_volume and data_root is None:
            raise ValueError("data_volume needs data_root: where the volume is mounted in the runner")
        self.root, self.mirrors = root, mirrors
        self.data_volume, self.data_root, self.network = data_volume, data_root, network
        self.image, self.gateway_target, self.runtime = image, gateway_target, runtime
        self.repo_url, self.pr_ref, self.base_branch = repo_url, pr_ref, base_branch
        self.on_verdict, self.on_failed = on_verdict, on_failed
        self.egress_image, self.memory, self.cpus = egress_image, memory, cpus
        self.owner = owner
        self.labels = ["--label", f"doppel.owner={owner}"] if owner else []
        self.egress = f"doppel-egress-{secrets.token_hex(3)}"
        self.pods: dict[str, Pod] = {}

    # ---- the egress forwarder ------------------------------------------------------------------------

    def _ensure_egress(self) -> None:
        if docker("ps", "-q", "--filter", f"name=^{self.egress}$"):
            return
        where = ["--network", self.network] if self.network else ["--add-host", "host.docker.internal:host-gateway"]
        docker("run", "-d", "--rm", "--name", self.egress, *self.labels, *where,
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               self.egress_image, "python", "-c", EGRESS_SCRIPT, self.gateway_target)  # fmt: skip

    def _mount(self, folder: Path, target: str) -> list[str]:
        """A pod's read-only view of one of the runner's folders: a host bind, or a subpath of the runner's volume."""
        if not self.data_volume:
            return ["-v", f"{folder}:{target}:ro"]
        sub = folder.resolve().relative_to(self.data_root.resolve()).as_posix()
        return ["--mount", f"type=volume,src={self.data_volume},dst={target},volume-subpath={sub},readonly"]

    # ---- the Sandbox port ----------------------------------------------------------------------------

    def create_pod(self, pr: PRRef) -> str:
        self._ensure_egress()
        pod_id = f"doppel-pod-{secrets.token_hex(4)}"
        folder = self.root / pod_id
        for sub in ("work", "job"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
            (folder / sub).chmod(0o755)
        network = pod_id
        docker("network", "create", "--internal", *self.labels, network)
        try:
            docker("network", "connect", "--alias", "gateway", network, self.egress)
            # gVisor cannot reach Docker's embedded DNS. Use the forwarder's address on this pod's own network.
            networks = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", self.egress))
            gateway_ip = IPv4Address(networks[network]["IPAddress"])
            docker(
                "run", "-d", "--init", "--name", pod_id, "--network", network,  # --init: a PID 1 that reaps orphans
                "--add-host", f"gateway:{gateway_ip}",
                *(["--runtime", self.runtime] if self.runtime else []),
                "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", "512",
                *self._mount(folder / "work", "/work"), *self._mount(folder / "job", "/job"),
                "--label", f"doppel.pr={pr}", *self.labels, self.image,
            )  # fmt: skip
        except Exception:
            with suppress(OSError, subprocess.TimeoutExpired):
                self.delete_pod(pod_id)
            raise
        self.pods[pod_id] = Pod(pod_id, pr, network, folder)
        log.info("pod %s for %s", pod_id, pr)
        return pod_id

    def start_reviewer(self, pod_id: str, job: ReviewJob) -> None:
        pod = self.pods[pod_id]
        free = sorted(set(range(1, UID_POOL + 1)) - set(pod.uids.values()))
        if not free:
            self.on_failed(job.slot_id, "no free reviewer user in the pod")
            return
        pod.uids[job.slot_id] = free[0]
        pod.runs[job.slot_id] = asyncio.get_running_loop().create_task(self._run(pod, job))

    def stop_reviewer(self, pod_id: str, slot_id: int) -> None:
        pod = self.pods.get(pod_id)
        if pod is None or slot_id not in pod.uids:
            return
        uid = UID_BASE + pod.uids[slot_id]
        killed = subprocess.run(["docker", "exec", "--user", str(uid), pod_id, "pkill", "-KILL", "-u", str(uid)],
                                capture_output=True, text=True, timeout=60)  # fmt: skip
        log.info("pod %s: pkill uid %s -> exit %s %s", pod_id, uid, killed.returncode, killed.stderr.strip())
        task = pod.runs.pop(slot_id, None)
        if task is not None:
            task.cancel()
        pod.uids.pop(slot_id, None)
        log.info("pod %s: stopped slot %s", pod_id, slot_id)

    def delete_pod(self, pod_id: str) -> None:
        pod = self.pods.pop(pod_id, None)
        for task in pod.runs.values() if pod else ():
            task.cancel()
        docker("rm", "-f", pod_id, check=False)
        docker("network", "disconnect", "-f", pod_id, self.egress, check=False)
        docker("network", "rm", pod_id, check=False)
        shutil.rmtree(self.root / pod_id, ignore_errors=True)
        log.info("pod %s deleted", pod_id)

    def sweep(self) -> None:
        """Remove what an earlier process with this owner left behind: pods, forwarders, networks."""
        if not self.owner:
            return
        label = f"label=doppel.owner={self.owner}"
        containers = docker("ps", "-aq", "--filter", label, check=False).split()
        if containers:
            docker("rm", "-f", *containers, check=False)
        networks = docker("network", "ls", "-q", "--filter", label, check=False).split()
        for network in networks:
            docker("network", "rm", network, check=False)
        if containers or networks:
            log.info("swept %d container(s) and %d network(s) left by an earlier process",
                     len(containers), len(networks))  # fmt: skip

    def close(self) -> None:
        for pod_id in list(self.pods):
            self.delete_pod(pod_id)
        docker("rm", "-f", self.egress, check=False)

    # ---- one reviewer run ----------------------------------------------------------------------------

    def _prepare(self, pod: Pod, job: ReviewJob) -> Path:
        """Check out the head (once per head) and write the job: never the capability."""
        repo = job.pr.repo if job.pr else pod.pr.repo
        pr = job.pr or pod.pr
        self.mirrors.sync(repo, self.repo_url(pr), pr.number, self.pr_ref(pr))
        self.mirrors.checkout(repo, job.head_sha, pod.folder / "work" / job.head_sha)
        folder = pod.folder / "job" / str(job.slot_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "diff.patch").write_text(self.mirrors.diff(repo, job.head_sha, job.base_ref or self.base_branch(pr)))
        (folder / "job.json").write_text(json.dumps({
            "reviewer": job.reviewer, "pr": pr.number, "head_sha": job.head_sha,
            "workdir": f"/work/{job.head_sha}", "gateway_url": f"http://gateway:{EGRESS_PORT}",
            "model": job.model, "instructions": job.instructions, "focus": job.focus,
        }))  # fmt: skip
        for path in (folder, folder / "diff.patch", folder / "job.json"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        write_doppel_files(folder / "doppel", job.files or {})
        return folder

    async def _run(self, pod: Pod, job: ReviewJob) -> None:
        uid = UID_BASE + pod.uids[job.slot_id]
        try:
            await asyncio.to_thread(self._prepare, pod, job)
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "--user", str(uid), "--workdir", f"/work/{job.head_sha}",
                "-e", "AGENT_CAPABILITY", pod.id, "python", "-m", "doppel_sandbox", f"/job/{job.slot_id}",
                env={**os.environ, "AGENT_CAPABILITY": job.capability},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
            out, err = await proc.communicate()
            lines = out.decode(errors="replace").strip().splitlines()
            verdict = json.loads(lines[-1]) if proc.returncode == 0 and lines else None
            if isinstance(verdict, dict):
                self.on_verdict(job.slot_id, verdict)
            else:
                self.on_failed(job.slot_id, err.decode(errors="replace").strip()[-500:] or f"exit {proc.returncode}")
        except asyncio.CancelledError:
            raise  # stopped: the scheduler already knows
        except Exception as e:
            log.exception("pod %s: slot %s failed", pod.id, job.slot_id)
            self.on_failed(job.slot_id, f"{type(e).__name__}: {e}"[:500])
        finally:
            pod.uids.pop(job.slot_id, None)
            pod.runs.pop(job.slot_id, None)
