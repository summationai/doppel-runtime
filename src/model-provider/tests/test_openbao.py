"""The durable copy against a real OpenBao dev server. Runs when `bao` is installed; skipped otherwise."""

import shutil
import socket
import subprocess
import time
import urllib.request

import pytest
from doppel_model_provider.openbao import OpenBaoStore, StoreError, policy_name

pytestmark = pytest.mark.skipif(shutil.which("bao") is None, reason="needs the OpenBao CLI (`bao`)")
ROOT = "dev-root"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def bao():
    port = free_port()
    addr = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["bao", "server", "-dev", f"-dev-root-token-id={ROOT}", f"-dev-listen-address=127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{addr}/v1/sys/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        req = urllib.request.Request(
            f"{addr}/v1/sys/mounts/kv", method="POST", headers={"X-Vault-Token": ROOT},
            data=b'{"type": "kv", "options": {"version": "2"}}',
        )  # fmt: skip
        urllib.request.urlopen(req, timeout=5)
        yield addr
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture
async def store(bao):
    s = OpenBaoStore(bao, ROOT)
    yield s
    await s.close()


async def test_save_load_list_delete(store):
    assert await store.load("u-steven") is None
    await store.save("u-steven", '{"tokens": "x"}')
    await store.save("key:summation", '{"OPENAI_API_KEY": "y"}')
    assert await store.load("u-steven") == '{"tokens": "x"}'
    assert {"u-steven", "key:summation"} <= set(await store.payers())
    await store.delete("u-steven")
    assert await store.load("u-steven") is None


async def test_dsv_13_a_payers_token_reaches_their_path_and_no_one_elses(store):
    await store.save("u-steven", "stevens")
    await store.save("u-troy", "troys")
    steven = await store.scoped_token("u-steven")
    status, data = await store._call("GET", "kv/data/users/u-steven/codex", steven)
    assert status == 200 and data["data"]["data"]["value"] == "stevens"
    status, _ = await store._call("GET", "kv/data/users/u-troy/codex", steven)
    assert status == 403
    status, _ = await store._call("LIST", "kv/metadata/users", steven)
    assert status == 403  # can't even see who else is there
    status, _ = await store._call("POST", "kv/data/users/u-troy/codex", steven, {"data": {"value": "overwritten"}})
    assert status == 403
    assert await store.load("u-troy") == "troys"


async def test_scoped_tokens_are_revoked_after_use(store):
    await store.save("u-steven", "stevens")
    token = await store.scoped_token("u-steven")
    status, _ = await store._call("POST", "auth/token/revoke-self", token)
    assert status == 403  # no default policy: it can't even revoke itself
    status, _ = await store._call("GET", "kv/data/users/u-steven/codex", token)
    assert status == 200
    await store.revoke(token)
    status, _ = await store._call("GET", "kv/data/users/u-steven/codex", token)
    assert status == 403


def test_policy_names_are_safe():
    assert policy_name("key:summation") == "doppel-user-key-summation"


async def test_an_unreachable_openbao_is_a_store_error():
    s = OpenBaoStore(f"http://127.0.0.1:{free_port()}", ROOT)
    with pytest.raises(StoreError):
        await s.load("u-steven")
    await s.close()


async def test_every_operation_leaves_no_live_token(store, bao):
    """Each load and save mints a scoped token and revokes it: none survive the operation."""
    status, before = await store._call("LIST", "auth/token/accessors", ROOT)
    await store.save("u-steven", "x")
    await store.load("u-steven")
    status, after = await store._call("LIST", "auth/token/accessors", ROOT)
    assert status == 200 and set(after["data"]["keys"]) == set(before["data"]["keys"])
