from __future__ import annotations

import argparse
import logging


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="shiori")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── ingest (with subcommands) ──────────────────────────────────────
    p_ingest = sub.add_parser(
        "ingest",
        help="sync docs and issue/PR to build index",
        description="""
        Subcommands:
          fetch  — API fetch + git pull only (no chunk/embed)
          index  — chunk + embed from issue_items/doc_files
          run    — fetch + index sequentially (default)

        Run without a subcommand (e.g. 'shiori ingest --repo ...') for
        backward-compatible combined behavior.
        """,
    )
    # Ingest without subcommand: use sub-subparsers with a default action.
    # We use a second level of subparsers for fetch/index/run.
    ingest_sub = p_ingest.add_subparsers(dest="ingest_action")

    # fetch
    p_fetch = ingest_sub.add_parser("fetch", help="API fetch + git pull only (no chunk/embed)")
    p_fetch.add_argument("--repo", action="append", help="owner/name (multiple allowed, defaults to SHIORI_REPOS)")
    p_fetch.add_argument("--rebuild", action="store_true", help=argparse.SUPPRESS)

    # index
    p_index = ingest_sub.add_parser("index", help="chunk + embed from issue_items/doc_files")
    p_index.add_argument("--repo", action="append", help="owner/name (multiple allowed, defaults to SHIORI_REPOS)")
    p_index.add_argument("--rebuild", action="store_true", help=argparse.SUPPRESS)

    # run
    p_run = ingest_sub.add_parser("run", help="fetch + index sequentially (default behavior)")
    p_run.add_argument("--repo", action="append", help="owner/name (multiple allowed, defaults to SHIORI_REPOS)")
    p_run.add_argument("--rebuild", action="store_true", help="discard index and rebuild all")

    # Backward-compatible: ingest without subcommand uses the same args as run
    p_ingest.add_argument("--repo", action="append", help="owner/name (multiple allowed, defaults to SHIORI_REPOS)")
    p_ingest.add_argument("--rebuild", action="store_true", help="discard index and rebuild all")

    # ── forget ────────────────────────────────────────────────────────
    p_forget = sub.add_parser(
        "forget",
        help="drop a repo from the index (rows + clone), without touching other repos",
    )
    p_forget.add_argument(
        "--repo",
        action="append",
        required=True,
        help="owner/name (multiple allowed). Need not be in SHIORI_REPOS",
    )
    p_forget.add_argument(
        "--keep-clone",
        action="store_true",
        help="delete indexed rows but keep the local git clone",
    )

    # ── serve ─────────────────────────────────────────────────────────
    p_serve = sub.add_parser("serve", help="start MCP server")
    p_serve.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="http=streamable HTTP (compose default) / stdio=local direct connection",
    )

    args = parser.parse_args()
    if args.command == "ingest":
        from .ingest import run_fetch, run_index, run_ingest

        # Route subcommands
        ingest_action = getattr(args, "ingest_action", None)
        if ingest_action == "fetch":
            run_fetch(repos=args.repo)
        elif ingest_action == "index":
            run_index(repos=args.repo, rebuild=getattr(args, "rebuild", False))
        elif ingest_action == "run":
            run_ingest(repos=args.repo, rebuild=getattr(args, "rebuild", False))
        else:
            # No subcommand: backward compatible (equivalent to "run")
            run_ingest(repos=args.repo, rebuild=args.rebuild)
    elif args.command == "forget":
        from .ingest import run_forget

        result = run_forget(repos=args.repo, keep_clone=args.keep_clone)
        for repo, deleted in result.items():
            print(f"{repo}: {sum(deleted.values())} rows deleted")
            for table, n in deleted.items():
                print(f"  {table}: {n}")
    elif args.command == "serve":
        from .mcp_server import run

        run("streamable-http" if args.transport == "http" else "stdio")


if __name__ == "__main__":
    main()
