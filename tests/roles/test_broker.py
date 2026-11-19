import json

import pytest

from doppel_runtime.ports.credentials import NotSignedIn
from doppel_runtime.roles.broker import CHATGPT_BASE, OPENAI_BASE, CodexHomes


def write(home, auth: dict):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(auth))


async def test_a_chatgpt_sign_in(tmp_path):
    write(tmp_path / "u-steven", {"tokens": {"access_token": "secret-at-9f3", "account_id": "acct"}})
    cred = await CodexHomes(tmp_path).credential("u-steven")
    assert cred.base_url == CHATGPT_BASE
    assert cred.headers == {"authorization": "Bearer secret-at-9f3", "chatgpt-account-id": "acct"}
    assert "secret-at-9f3" not in repr(cred)


async def test_an_api_key(tmp_path):
    write(tmp_path / "key:summation", {"OPENAI_API_KEY": "sk-test"})
    cred = await CodexHomes(tmp_path).credential("key:summation")
    assert (cred.base_url, cred.headers) == (OPENAI_BASE, {"authorization": "Bearer sk-test"})


async def test_an_explicit_home(tmp_path):
    write(tmp_path / "elsewhere", {"tokens": {"access_token": "at"}})
    homes = CodexHomes(tmp_path / "homes", {"u-steven": tmp_path / "elsewhere"})
    assert (await homes.credential("u-steven")).headers == {"authorization": "Bearer at"}


@pytest.mark.parametrize("payer", ["u-nobody", "../etc", ".hidden"])
async def test_no_sign_in(tmp_path, payer):
    with pytest.raises(NotSignedIn):
        await CodexHomes(tmp_path).credential(payer)


async def test_signed_out(tmp_path):
    write(tmp_path / "u-steven", {"tokens": None, "OPENAI_API_KEY": None})
    with pytest.raises(NotSignedIn):
        await CodexHomes(tmp_path).credential("u-steven")
