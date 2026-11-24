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


def test_tenants_reviewers_and_the_review_settings(tmp_path):
    from datetime import timedelta

    f = tmp_path / "c.toml"
    f.write_text(TENANTS)
    cfg = load(f, env={})
    assert cfg.github.repo_url.format(repo="o/r") == "/srv/repos/o/r"
    assert (cfg.intake.poll, cfg.intake.calls_per_hour) == (timedelta(seconds=30), 500)
    assert cfg.scheduler.settle == timedelta(0) and cfg.scheduler.payer_cap == 2
    assert cfg.sandbox.runtime == "runsc" and cfg.sandbox.image == "doppel-sandbox:dev"
    [t] = cfg.tenants
    assert t.repos == ("summationai/billing", "summationai/web")
    assert [r.reviewer.handle for r in t.reviewers] == ["steven", "repo-owner"]


def test_the_directory_from_the_config(tmp_path):
    from doppel_control_plane.directory import ConfigDirectory

    f = tmp_path / "c.toml"
    f.write_text(TENANTS)
    signed = {"summation/u-steven"}
    d = ConfigDirectory(load(f, env={}).tenants, lambda payer: payer in signed)
    assert [r.handle for r in d.reviewers("summation", "summationai/billing")] == ["steven", "repo-owner"]
    assert [r.handle for r in d.reviewers("summation", "summationai/web")] == ["steven"]  # repo-owner: billing only
    assert d.reviewers("summation", "summationai/not-listed") == []
    assert d.reviewers("acme", "summationai/billing") == []
    assert d.personal_reviewer("summation", "stevenliu").handle == "steven"  # GitHub logins ignore case
    assert d.personal_reviewer("acme", "stevenliu") is None
    assert d.app_payer("summation", "summationai/billing", "repo-owner") == "summation/u-steven"
    assert d.signed_in("summation/u-steven") and not d.signed_in("summation/u-troy")


def test_a_bad_directory_is_refused(tmp_path):
    import pytest

    f = tmp_path / "c.toml"
    one = '[[tenants]]\nid="s"\n[[tenants.reviewers]]\nhandle="a"\n'
    for bad, why in [
        (one + 'kind="personal"\nowner="s/u-a"\n', "owner and a login"),
        (one + 'kind="app"\npayer="acme/u-x"\n', "another tenant"),
        (one + 'kind="app"\n[[tenants.reviewers]]\nhandle="a"\nkind="app"\n', "unique"),
        ('[github]\nmode="webhooks"\n', "off or app"),
    ]:  # fmt: skip
        f.write_text(bad)
        with pytest.raises(ValueError, match=why):
            load(f, env={})
