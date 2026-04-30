"""claude-tg CLI: wrap claude with Telegram remote control."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from claude_tg.config import ConfigError, load_config
from claude_tg.session import Session


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "claude-tg" / "config.toml"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-tg",
        description="Wrap `claude` with Telegram remote control.",
    )
    parser.add_argument("prompt", nargs="?", default="",
                        help="Initial prompt (same as claude's positional arg)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH})")
    args, passthrough = parser.parse_known_args()

    if not args.prompt:
        parser.error("initial prompt is required (arg or first positional)")

    try:
        cfg = load_config(Path(args.config))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("claude binary not found on PATH", file=sys.stderr)
        sys.exit(2)

    # initial_prompt is delivered to claude via stream-json, not via argv.
    argv = [claude_bin] + passthrough
    session = Session(
        config=cfg,
        claude_argv=argv,
        initial_prompt=args.prompt,
    )

    try:
        asyncio.run(session.run())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    sys.exit(session.exit_code or 0)


if __name__ == "__main__":
    main()
