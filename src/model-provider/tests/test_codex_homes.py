import json
import stat

import pytest
from doppel_model_provider.codex_homes import CHATGPT_BASE, OPENAI_BASE, CodexHomes
from doppel_shared.ports.credentials import NotSignedIn


def write(homes: CodexHomes, payer: str, auth: dict):
    homes.make_home(payer)
    (homes.home(payer) / "auth.json").write_text(json.dumps(auth))


def test_the_layout_is_tenant_user_codex(tmp_path):
    homes = CodexHomes(tmp_path / "tenants")
    assert homes.home("summation/u-steven") == tmp_path / "tenants" / "summation" / "u-steven" / "codex"


def test_homes_are_owner_only(tmp_path):
    homes = CodexHomes(tmp_path / "tenants")
    home = homes.make_home("summation/u-steven")
    for folder in (homes.root, home.parent.parent, home.parent, home):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_payers_lists_every_home_on_disk(tmp_path):
    homes = CodexHomes(tmp_path / "tenants")
    for payer in ("summation/u-steven", "summation/u-troy", "acme/u-ann"):
        homes.make_home(payer)
    assert homes.payers() == ["acme/u-ann", "summation/u-steven", "summation/u-troy"]


async def test_a_chatgpt_sign_in(tmp_path):
    homes = CodexHomes(tmp_path)
    write(homes, "summation/u-steven", {"tokens": {"access_token": "secret-at-9f3", "account_id": "acct"}})
    cred = await homes.credential("summation/u-steven")
    assert cred.base_url == CHATGPT_BASE
    assert cred.headers == {"authorization": "Bearer secret-at-9f3", "chatgpt-account-id": "acct"}
    assert "secret-at-9f3" not in repr(cred)


async def test_an_api_key(tmp_path):
    homes = CodexHomes(tmp_path)
    write(homes, "summation/tenant-key", {"OPENAI_API_KEY": "sk-test"})
    cred = await homes.credential("summation/tenant-key")
    assert (cred.base_url, cred.headers) == (OPENAI_BASE, {"authorization": "Bearer sk-test"})


async def test_an_explicit_home(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "at"}}))
    homes = CodexHomes(tmp_path / "tenants", {"summation/u-steven": tmp_path / "elsewhere"})
    assert (await homes.credential("summation/u-steven")).headers == {"authorization": "Bearer at"}


@pytest.mark.parametrize("payer", ["summation/u-nobody", "u-steven", "../etc/passwd", "summation/.hidden"])
async def test_no_sign_in(tmp_path, payer):
    with pytest.raises(NotSignedIn):
        await CodexHomes(tmp_path).credential(payer)


async def test_signed_out(tmp_path):
    homes = CodexHomes(tmp_path)
    write(homes, "summation/u-steven", {"tokens": None, "OPENAI_API_KEY": None})
    with pytest.raises(NotSignedIn):
        await homes.credential("summation/u-steven")


def test_make_home_creates_missing_parent_folders(tmp_path):
    """Found by the live check: the data folder needn't exist yet."""
    homes = CodexHomes(tmp_path / "not" / "yet" / "tenants")
    home = homes.make_home("summation/u-steven")
    assert home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700
