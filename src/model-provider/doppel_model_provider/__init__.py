"""The model provider (the broker): each payer's model sign-in, kept in their own Codex home.

    codex_homes   a payer's home -> its auth.json -> a credential (the read path)
    (checkpoint 3) device-code sign-in, refresh under a per-payer lock, the durable copy in OpenBao

Imports only `doppel_shared`. It implements the `Credentials` port the gateway uses.
"""

from doppel_model_provider.codex_homes import CHATGPT_BASE, OPENAI_BASE, CodexHomes

__all__ = ["CHATGPT_BASE", "OPENAI_BASE", "CodexHomes"]
