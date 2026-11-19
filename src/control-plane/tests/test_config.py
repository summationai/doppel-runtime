from pathlib import Path

from doppel_control_plane.config import load


def test_defaults_bind_everything_to_localhost():
    cfg = load(env={})
    assert cfg.data_dir == Path("./var")
    assert {name: (lis.host, lis.port) for name, lis in cfg.listeners.items()} == {
        "model": ("127.0.0.1", 8700),
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
