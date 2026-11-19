"""The model provider (the broker): each payer's model sign-in, kept in their own Codex home.

    codex_homes   <data_dir>/tenants/<tenant>/<user>/codex/auth.json -> a credential
    codex_sdk     the Codex SDK calls: device-code sign-in, refresh (Codex does both itself)
    broker        credential, refresh under a per-payer lock, keep_fresh, login, status
    api           the broker's routes, for the internal listener only

A payer id is `<tenant>/<user>`. Imports only `doppel_shared`. The broker implements the
`Credentials` port the gateway uses.
"""

from doppel_model_provider.broker import Broker, DeviceCode
from doppel_model_provider.codex_homes import CHATGPT_BASE, OPENAI_BASE, CodexHomes
from doppel_model_provider.codex_sdk import CodexSdkAccount

__all__ = ["CHATGPT_BASE", "OPENAI_BASE", "Broker", "CodexHomes", "CodexSdkAccount", "DeviceCode"]
