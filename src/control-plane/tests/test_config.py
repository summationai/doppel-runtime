from pathlib import Path

from doppel_control_plane.config import load


def test_defaults_use_automatic_model_binding_and_loopback_for_private_listeners():
    cfg = load(env={})
    assert cfg.data_dir == Path("./var")
    assert {name: (lis.host, lis.port) for name, lis in cfg.listeners.items()} == {
        "model": (None, 8700),
        "internal": ("127.0.0.1", 8701),
        "health": ("127.0.0.1", 8702),
    }


def test_file_then_env(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('data_dir = "/srv/doppel"\n[listeners.model]\nhost = "10.0.0.1"\nport = 9000\n')
    cfg = load(f, env={"DOPPEL_MODEL_PORT": "0", "DOPPEL_INTERNAL_HOST": "127.0.0.2"})
    assert cfg.data_dir == Path("/srv/doppel")
    assert (cfg.listeners["model"].host, cfg.listeners["model"].port) == ("10.0.0.1", 0)
    assert cfg.listeners["internal"].host == "127.0.0.2"
    assert cfg.ledger_path == Path("/srv/doppel/ledger.db")


TENANTS = """
[github]
mode = "off"
repo_url = "/srv/repos/{repo}"

[intake]
poll_seconds = 30
calls_per_hour = 500

[scheduler]
settle_seconds = 0

[sandbox]
runtime = "runsc"

[[tenants]]
id = "summation"
repos = ["summationai/billing", "summationai/web"]

  [[tenants.reviewers]]
  handle = "steven"
  kind = "personal"
  owner = "summation/u-steven"
  login = "StevenLiu"

  [[tenants.reviewers]]
  handle = "repo-owner"
  kind = "app"
  payer = "summation/u-steven"
  repos = ["summationai/billing"]
"""


def test_a_runner_reads_its_cap_and_where_it_fetches_and_ignores_the_old_standalone_keys(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[scheduler]\nhost_cap = 3\nsettle_seconds = 9\n[github]\nmode = "app"\nrepo_url = "file:///repos/{repo}"\n'
                 '[[tenants]]\nid = "summation"\n')  # fmt: skip
    cfg = load(f, env={})
    assert (cfg.host_cap, cfg.repo_url) == (3, "file:///repos/{repo}")
    assert load(env={}).repo_url == "https://github.com/{repo}.git"
