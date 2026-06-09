from __future__ import annotations

import argparse
import logging


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="shiori")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="docs と issue/PR を同期して索引する")
    p_ingest.add_argument("--repo", action="append", help="owner/name（複数可。省略時は SHIORI_REPOS）")
    p_ingest.add_argument("--rebuild", action="store_true", help="索引を破棄して全件作り直す")

    p_serve = sub.add_parser("serve", help="MCP サーバーを起動する")
    p_serve.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="http=streamable HTTP（compose 既定） / stdio=ローカル直接続",
    )

    args = parser.parse_args()
    if args.command == "ingest":
        from .ingest import run_ingest

        run_ingest(repos=args.repo, rebuild=args.rebuild)
    elif args.command == "serve":
        from .mcp_server import run

        run("streamable-http" if args.transport == "http" else "stdio")


if __name__ == "__main__":
    main()
