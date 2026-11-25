"""The pod manager's Docker arguments where Docker itself isn't needed (tests/integration runs the real thing)."""

from pathlib import Path

import pytest
from doppel_control_plane import pods
from doppel_control_plane.mirrors import Mirrors


def make(tmp_path: Path, **kw) -> pods.DockerPods:
    return pods.DockerPods(
        tmp_path / "data" / "pods", Mirrors(tmp_path / "data" / "mirrors"), image="doppel-sandbox:test",
        gateway_target="doppel-runtime:8700", repo_url=lambda pr: "", on_verdict=lambda *_: None,
        on_failed=lambda *_: None, **kw,
    )  # fmt: skip


def test_on_a_host_a_pods_folders_are_bind_mounts(tmp_path):
    p = make(tmp_path)
    folder = tmp_path / "data" / "pods" / "doppel-pod-1" / "work"
    assert p._mount(folder, "/work") == ["-v", f"{folder}:/work:ro"]


def test_in_a_container_a_pods_folders_are_subpaths_of_the_runners_volume(tmp_path):
    p = make(tmp_path, data_volume="doppel-runtime-data", data_root=tmp_path / "data", network="doppel-runtime")
    folder = tmp_path / "data" / "pods" / "doppel-pod-1" / "job"
    assert p._mount(folder, "/job") == [
        "--mount", "type=volume,src=doppel-runtime-data,dst=/job,volume-subpath=pods/doppel-pod-1/job,readonly",
    ]  # fmt: skip
    with pytest.raises(ValueError):
        p._mount(tmp_path / "elsewhere", "/job")  # never a path outside the volume
    with pytest.raises(ValueError, match="data_root"):
        make(tmp_path, data_volume="doppel-runtime-data")


@pytest.mark.parametrize(
    ("configured", "docker_says", "expected"),
    [
        ("auto", '{"io.containerd.runc.v2":{},"runc":{},"runsc":{"path":"/usr/bin/runsc"}}', "runsc"),
        ("auto", '{"runc":{}}', None),
        ("auto", "", None),  # Docker didn't answer: the standard runtime
        ("auto", "not json", None),
        ("runsc", "unused", "runsc"),  # as configured, without asking Docker
        (None, "unused", None),
    ],
)
def test_auto_uses_gvisor_only_where_docker_has_it(monkeypatch, configured, docker_says, expected):
    monkeypatch.setattr(pods, "docker", lambda *a, **k: docker_says)
    assert pods.detect_runtime(configured) == expected
    assert pods.isolation(expected) == ("gvisor" if expected == "runsc" else "standard")
