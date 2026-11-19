"""`doppel-runtime serve [--config PATH]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from doppel_runtime import app, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doppel-runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the runtime process")
    serve.add_argument("--config", type=Path, help="a TOML config file (see doppel_runtime.config)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "serve":
        asyncio.run(app.serve(config.load(args.config)))
    return 0
