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
    p_fetch.add_argument("--repo", action="append", default=argparse.SUPPRESS, help="owner/name (multiple allowed)")
    p_fetch.add_argument("--rebuild", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_fetch.add_argument("--backfill-since", default=argparse.SUPPRESS, help="YYYY-MM-DD: seed cursors for initial backfill of new repos")

    # index
    p_index = ingest_sub.add_parser("index", help="chunk + embed from issue_items/doc_files")
    p_index.add_argument("--repo", action="append", default=argparse.SUPPRESS, help="owner/name (multiple allowed)")
    p_index.add_argument("--rebuild", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_index.add_argument(
        "--all",
        action="store_true",
        default=argparse.SUPPRESS,
        help="index every repo in SHIORI_REPOS "
             "(the resume path for a killed reindex drain, issue #352)",
    )

    # run
    p_run = ingest_sub.add_parser("run", help="fetch + index sequentially (default behavior)")
    p_run.add_argument("--repo", action="append", default=argparse.SUPPRESS, help="owner/name (multiple allowed)")
    p_run.add_argument("--rebuild", action="store_true", default=argparse.SUPPRESS, help="discard index and rebuild all")
    p_run.add_argument("--backfill-since", default=argparse.SUPPRESS, help="YYYY-MM-DD: seed cursors for initial backfill of new repos")

    # reindex (issue #352): rebuild chunks, keep fetched raw data
    p_reindex = ingest_sub.add_parser(
        "reindex",
        help="rebuild chunks (re-chunk + re-embed) while keeping fetched raw data",
        description="""
        Clears the chunks table (and the doc_files sha cache) and re-runs
        the index phase, WITHOUT touching issue_items bodies/comments,
        sync_state cursors, sync_runs, or repo_index_state -- so no GitHub
        re-fetch is needed. --repo scopes to specific repos; omit --repo to
        reindex every repo in SHIORI_REPOS.

        A reindex killed mid-drain is resumed with 'shiori ingest index --all'.
        """,
    )
    p_reindex.add_argument(
        "--repo",
        action="append",
        default=argparse.SUPPRESS,
        help="owner/name (multiple allowed); omit to reindex every configured repo",
    )

    # Backward-compatible: ingest without subcommand uses the same args as run
    p_ingest.add_argument("--repo", action="append", help="owner/name (multiple allowed)")
    p_ingest.add_argument("--rebuild", action="store_true", help="discard index and rebuild all")
    p_ingest.add_argument("--backfill-since", help="YYYY-MM-DD: seed cursors for initial backfill of new repos")

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
        from .ingest import run_fetch, run_index, run_ingest, run_reindex

        ingest_action = getattr(args, "ingest_action", None)
        repos = getattr(args, "repo", None)

        # reindex is unscoped by default (repos=None reindexes every repo in
        # SHIORI_REPOS) -- unlike fetch/index/run (issue #338), it does not
        # require --repo.
        if ingest_action == "reindex":
            run_reindex(repos=repos)
            return

        # `index --all` is the documented resume path for a killed reindex
        # drain (issue #352): unscoped on purpose, so it bypasses the --repo
        # guard below.
        index_all = getattr(args, "all", False)
        if index_all and repos:
            p_ingest.error("--all and --repo are mutually exclusive")
        if ingest_action == "index" and index_all:
            run_index(repos=None, rebuild=getattr(args, "rebuild", False))
            return

        # Validate --repo: must be present somewhere (parent or subcommand)
        if not repos:
            p_ingest.error("the following arguments are required: --repo")

        # Route subcommands
        rebuild = getattr(args, "rebuild", False)
        backfill_since = getattr(args, "backfill_since", None)

        if ingest_action == "fetch":
            run_fetch(repos=repos, backfill_since=backfill_since)
        elif ingest_action == "index":
            run_index(repos=repos, rebuild=rebuild)
        elif ingest_action == "run":
            run_ingest(repos=repos, rebuild=rebuild, backfill_since=backfill_since)
        else:
            # No subcommand: backward compatible (equivalent to "run")
            run_ingest(repos=repos, rebuild=rebuild, backfill_since=backfill_since)
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
