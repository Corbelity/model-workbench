"""Command-line entry point: `corbelity-workbench` or `python -m corbelity.workbench`."""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def is_loopback(host: str) -> bool:
    """True when `host` only accepts connections from this machine."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        # A hostname other than localhost, or something unparseable: assume it is reachable.
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="corbelity-workbench",
        description="Run the Corbelity Model Workbench.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to bind (default {DEFAULT_HOST}, this machine only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--reload", action="store_true",
                        help="restart on code changes, and stop the browser caching assets")
    args = parser.parse_args(argv)

    if args.reload:
        # Reloading the server is only half of it: without this the browser keeps serving
        # the previous style.css and app.js from its own cache, so a frontend edit appears
        # not to have taken. Set through the environment because --reload runs the app in a
        # subprocess, which inherits it. See DEV_MODE in app.py.
        os.environ["WORKBENCH_DEV"] = "1"

    if not is_loopback(args.host):
        # Said loudly and up front because the consequences are not obvious from the flag:
        # the workbench has no authentication, /api/traces serves every prompt and response
        # ever recorded, and the server fetches any image URL it is handed.
        print(
            f"WARNING: binding to {args.host!r} exposes the workbench beyond this machine.\n"
            "  It has no authentication. Anyone who can reach it can spend your API keys,\n"
            "  read recorded traces (full prompts and responses) and make this server fetch\n"
            "  arbitrary URLs. Use it only on a network you fully trust.",
            file=sys.stderr,
        )

    uvicorn.run("corbelity.workbench.app:app", host=args.host, port=args.port,
                reload=args.reload)


if __name__ == "__main__":
    main()
