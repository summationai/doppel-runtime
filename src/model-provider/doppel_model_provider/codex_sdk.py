"""The Codex SDK calls the broker makes. Codex itself signs in and refreshes: this code never calls
OpenAI's token endpoint, so it follows whatever Codex does as that changes.

`Account` is the interface the broker uses; `CodexSdkAccount` is the real one (the SDK starts a
`codex app-server` for each call, in the payer's home). Tests use a fake.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Credentials in a file (the broker reads and saves it), no update checks, analytics or feedback.
OVERRIDES = (
    'cli_auth_credentials_store="file"',
    "check_for_update_on_startup=false",
    "analytics.enabled=false",
    "feedback.enabled=false",
)


@dataclass(frozen=True)
class DeviceLogin:
    """A device-code sign-in in progress: the person opens the URL and enters the code."""

    verification_url: str
    user_code: str
    wait: Callable[[], Awaitable[bool]] = field(repr=False)  # True once they approved and auth.json is written


class Account(Protocol):
    async def start_device_login(self, home: Path) -> DeviceLogin: ...

    async def refresh(self, home: Path) -> None:
        """Codex runs the refresh-token flow and rewrites auth.json. Raises on failure."""
        ...


class CodexSdkAccount:
    @staticmethod
    def _config(home: Path):
        from openai_codex import CodexConfig

        return CodexConfig(config_overrides=OVERRIDES, env={"CODEX_HOME": str(home)})

    async def start_device_login(self, home: Path) -> DeviceLogin:
        from openai_codex import AsyncCodex

        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        codex = AsyncCodex(self._config(home))
        await codex.__aenter__()
        try:
            handle = await codex.login_chatgpt_device_code()
        except BaseException:
            await codex.close()
            raise

        async def wait() -> bool:
            try:
                done = await handle.wait()
                return bool(getattr(done, "success", True))
            finally:
                await codex.close()

        return DeviceLogin(handle.verification_url, handle.user_code, wait)

    async def refresh(self, home: Path) -> None:
        from openai_codex import AsyncCodex

        async with AsyncCodex(self._config(home)) as codex:
            await codex.account(refresh_token=True)
