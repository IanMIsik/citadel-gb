"""`citadel serve` -- thin wrapper so `pip install -e .` gives a console
script, same convention as the sibling gbpw project's `gbpw serve`.
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="citadel")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API + web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("citadel.api.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
