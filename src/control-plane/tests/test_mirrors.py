import subprocess

import pytest
from doppel_control_plane.mirrors import MirrorError, Mirrors


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    r = tmp_path / "origin"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "pr-7")
    (r / "a.py").write_text("x = 2\n")
    git(r, "commit", "-qam", "head")
    head = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")  # as a server would be: HEAD on the default branch
    return r, head


def test_sync_checkout_and_diff(tmp_path, origin):
    repo, head = origin
    m = Mirrors(tmp_path / "mirrors", partial=False)
    m.sync("summationai/sandbox", str(repo), 7, "refs/heads/pr-7")
    assert m.default_branch("summationai/sandbox") == "main"
    out = m.checkout("summationai/sandbox", head, tmp_path / "work" / head)
    assert (out / "a.py").read_text() == "x = 2\n"
    assert not (out / ".git").exists()
    diff = m.diff("summationai/sandbox", head)
    assert "-x = 1" in diff and "+x = 2" in diff


def test_a_new_head_is_fetched_into_the_same_mirror(tmp_path, origin):
    repo, head = origin
    m = Mirrors(tmp_path / "mirrors", partial=False)
    m.sync("summationai/sandbox", str(repo), 7, "refs/heads/pr-7")
    git(repo, "checkout", "-q", "pr-7")
    (repo / "a.py").write_text("x = 3\n")
    git(repo, "commit", "-qam", "head 2")
    head2 = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    m.sync("summationai/sandbox", str(repo), 7, "refs/heads/pr-7")
    assert (m.checkout("summationai/sandbox", head2, tmp_path / "w2") / "a.py").read_text() == "x = 3\n"


@pytest.mark.parametrize("repo", ["../x", "noslash", "a/b/c"])
def test_bad_repo_names_are_refused(tmp_path, repo):
    with pytest.raises(MirrorError):
        Mirrors(tmp_path).path(repo)


def test_the_diff_is_against_the_prs_base(tmp_path, origin):
    repo, head = origin
    m = Mirrors(tmp_path / "mirrors", partial=False)
    m.sync("summationai/sandbox", str(repo), 7, "refs/heads/pr-7")
    assert m.diff("summationai/sandbox", head, "main") == m.diff("summationai/sandbox", head)
    assert m.diff("summationai/sandbox", head, "pr-7") == ""
    with pytest.raises(MirrorError):
        m.diff("summationai/sandbox", head, "--output=/etc/x")


def test_bad_commits_are_refused(tmp_path, origin):
    repo, _ = origin
    m = Mirrors(tmp_path / "mirrors", partial=False)
    m.sync("summationai/sandbox", str(repo), 7, "refs/heads/pr-7")
    with pytest.raises(MirrorError):
        m.checkout("summationai/sandbox", "--output=/etc/x", tmp_path / "w")


def test_reviewers_preparing_at_once_dont_collide(tmp_path, origin):
    """Found in the demo: two reviewers starting together both cloned, then both fetched into one ref."""
    from concurrent.futures import ThreadPoolExecutor

    repo, head = origin
    m = Mirrors(tmp_path / "mirrors", partial=False)

    def prepare(_):
        m.sync("summationai/sandbox", str(repo), 7, "refs/heads/pr-7")
        return m.checkout("summationai/sandbox", head, tmp_path / "work" / head)

    with ThreadPoolExecutor(6) as pool:
        outs = list(pool.map(prepare, range(6)))
    assert {o for o in outs} == {tmp_path / "work" / head}
    assert (tmp_path / "work" / head / "a.py").read_text() == "x = 2\n"
