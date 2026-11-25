"""The release's own pieces (docs/specs/packaging.md "Releasing"): install.sh against a fake release on disk (file://,
so nothing is published or served), the version check, release.json, and the Homebrew formula."""

import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts/release/install.sh"
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts/release/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

OS = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
pytestmark = pytest.mark.skipif(OS is None or ARCH is None, reason="install.sh runs on a Mac or Linux")


def fake_release(root: Path, folder: str, version: str = "9.9.9", tamper: bool = False) -> Path:
    """A release laid out as GitHub serves one: <root>/<folder>/{doppel-runtime_<os>_<arch>, SHA256SUMS}."""
    d = root / folder
    d.mkdir(parents=True)
    binary = d / f"doppel-runtime_{OS}_{ARCH}"
    binary.write_text(f'#!/bin/sh\n[ "$1" = version ] && echo "doppel-runtime {version}"\n')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if tamper:
        digest = "0" * 64
    (d / "SHA256SUMS").write_text(f"{digest}  {binary.name}\n{'1' * 64}  install.sh\n")
    return d


def install(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DOPPEL_HOST_RELEASES": f"file://{tmp_path / 'rel'}",
            "DOPPEL_INSTALL_DIR": str(tmp_path / "bin")}  # fmt: skip
    return subprocess.run(["sh", str(INSTALL)], env={**base, **env}, capture_output=True, text=True, timeout=60)


def test_install_puts_the_checked_binary_in_place(tmp_path):
    fake_release(tmp_path / "rel", "latest/download")
    done = install(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "Installed doppel-runtime 9.9.9" in done.stdout
    installed = tmp_path / "bin" / "doppel-runtime"
    assert os.access(installed, os.X_OK)
    assert (
        subprocess.run([str(installed), "version"], capture_output=True, text=True).stdout.strip()
        == "doppel-runtime 9.9.9"
    )


def test_install_refuses_a_download_that_doesnt_match_its_sums(tmp_path):
    fake_release(tmp_path / "rel", "latest/download", tamper=True)
    done = install(tmp_path)
    assert done.returncode != 0 and "doesn't match the release's SHA256SUMS" in done.stderr
    assert not (tmp_path / "bin" / "doppel-runtime").exists()


def test_install_takes_a_named_version(tmp_path):
    fake_release(tmp_path / "rel", "download/v1.2.3", version="1.2.3")
    done = install(tmp_path, DOPPEL_RUNTIME_VERSION="1.2.3")
    assert done.returncode == 0, done.stderr
    assert "doppel-runtime 1.2.3" in done.stdout


def test_a_tag_must_match_the_runtime_version():
    v = release.runtime_version()
    assert release.check_version(f"v{v}") == v
    with pytest.raises(SystemExit, match="doesn't match"):
        release.check_version("v99.0.0")
    with pytest.raises(SystemExit, match="vX.Y.Z"):
        release.check_version("latest")


def test_release_json_names_images_by_digest_and_carries_the_protocol():
    m = release.manifest("0.2.0", "ghcr.io/x/runner@sha256:aa", "ghcr.io/x/sandbox@sha256:bb")
    assert m == {"version": "0.2.0", "protocol": release.protocol(), "runner_image": "ghcr.io/x/runner@sha256:aa",
                 "sandbox_image": "ghcr.io/x/sandbox@sha256:bb"}  # fmt: skip
    json.dumps(m)
    with pytest.raises(SystemExit, match="pinned by digest"):
        release.manifest("0.2.0", "ghcr.io/x/runner:0.2.0", "ghcr.io/x/sandbox@sha256:bb")


def test_the_formula_points_each_platform_at_its_binary_and_sum(tmp_path):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("".join(f"{str(i) * 64}  doppel-runtime_{o}_{a}\n" for i, (o, a) in enumerate(release.PLATFORMS)))
    rb = release.formula("0.2.0", sums)
    assert 'version "0.2.0"' in rb
    for i, (o, a) in enumerate(release.PLATFORMS):
        assert f'releases/download/v#{{version}}/doppel-runtime_{o}_{a}"\n      sha256 "{str(i) * 64}"' in rb
    if shutil.which("ruby"):
        checked = subprocess.run(["ruby", "-c"], input=rb, capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
    sums.write_text(f"{'0' * 64}  doppel-runtime_linux_amd64\n")
    with pytest.raises(SystemExit, match="missing"):
        release.formula("0.2.0", sums)
