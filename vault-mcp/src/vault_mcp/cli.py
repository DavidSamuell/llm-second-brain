"""`vault-mcp` command line (SPEC §6.3): `token create|list|revoke`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from vault_core.config import Settings, bind_addr_warning
from vault_core.policy import PolicyError
from vault_mcp.store import DEFAULT_EXPIRY, Credential, CredentialError, CredentialStore


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


def _row(cred: Credential, now: datetime) -> dict[str, object]:
    return {
        "id": cred.id,
        "name": cred.name,
        "owner": cred.owner,
        "kind": cred.kind,
        "scope": cred.scope,
        "folders": cred.folders,
        "token_prefix": cred.token_prefix,
        "status": cred.status(now),
        "created_at": cred.created_at.isoformat(),
        "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
        "last_used_at": cred.last_used_at.isoformat() if cred.last_used_at else None,
        "revoked_at": cred.revoked_at.isoformat() if cred.revoked_at else None,
    }


def _cmd_create(store: CredentialStore, args: argparse.Namespace) -> int:
    folders = [f.strip() for f in args.folders.split(",") if f.strip()] if args.folders else None
    token, cred = store.create_bearer(
        name=args.name, owner=args.owner, scope=args.scope, folders=folders, expires=args.expires
    )
    print(token)
    print(
        f"created {cred.name} ({cred.id}) for {cred.owner}: scope={cred.scope}"
        f" folders={','.join(cred.folders) if cred.folders else 'all'} expires={_fmt(cred.expires_at)}",
        file=sys.stderr,
    )
    print("The token is shown once. Store it now; only its hash is kept.", file=sys.stderr)
    return 0


def _cmd_list(store: CredentialStore, args: argparse.Namespace) -> int:
    now = store.now()
    creds = store.list_credentials()
    rows = [_row(c, now) for c in creds]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    header = (
        "ID",
        "NAME",
        "OWNER",
        "KIND",
        "SCOPE",
        "FOLDERS",
        "TOKEN",
        "STATUS",
        "EXPIRES",
        "LAST USED",
    )
    table = [header] + [
        (
            str(r["id"]),
            str(r["name"]),
            str(r["owner"]),
            str(r["kind"]),
            str(r["scope"]),
            ",".join(r["folders"]) if isinstance(r["folders"], list) else "all",
            f"vmcp_{r['token_prefix']}…" if r["token_prefix"] else "-",
            str(r["status"]),
            _fmt(c.expires_at),
            _fmt(c.last_used_at),
        )
        for r, c in zip(rows, creds, strict=True)
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    return 0


def _cmd_revoke(store: CredentialStore, args: argparse.Namespace) -> int:
    cred = store.revoke(args.credential)
    print(f"revoked {cred.name} ({cred.id})", file=sys.stderr)
    return 0


def _cmd_serve(store: CredentialStore, args: argparse.Namespace) -> int:
    from vault_mcp.server import build_server

    settings = Settings() if args.data_dir is None else Settings(data_dir=args.data_dir)
    if warning := bind_addr_warning(settings.bind_addr):
        print(f"WARNING: {warning}", file=sys.stderr)
    if args.stdio:
        token = os.environ.get("VAULT_MCP_TOKEN", "")
        if store.verify(token) is None:
            print(
                "error: VAULT_MCP_TOKEN is missing, invalid, expired, or revoked", file=sys.stderr
            )
            return 2
        build_server(settings, stdio_token=token).run(transport="stdio", show_banner=False)
    else:
        server = build_server(settings)
        server.run(transport="http", host=args.host, port=args.port, path="/mcp", show_banner=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault-mcp", description="Docs Vault Kit MCP gateway")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="data directory holding tokens.db (default: $DATA_DIR or /data)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    token = commands.add_parser("token", help="manage bearer tokens and OAuth grants")
    token_cmds = token.add_subparsers(dest="token_command", required=True)

    create = token_cmds.add_parser("create", help="create a bearer token (printed once)")
    create.add_argument("--name", required=True, help="unique label, e.g. alice-laptop")
    create.add_argument("--owner", required=True, help="owner email")
    create.add_argument("--scope", required=True, choices=["read", "write"])
    create.add_argument("--folders", help="comma-separated folder prefixes, e.g. wiki/,raw/")
    create.add_argument(
        "--expires",
        default=DEFAULT_EXPIRY,
        help=f"<n>d|<n>h|<n>m or never (default {DEFAULT_EXPIRY})",
    )
    create.set_defaults(handler=_cmd_create)

    lister = token_cmds.add_parser("list", help="list credentials (never shows tokens)")
    lister.add_argument("--json", action="store_true")
    lister.set_defaults(handler=_cmd_list)

    revoke = token_cmds.add_parser("revoke", help="revoke a credential by name or id")
    revoke.add_argument("credential")
    revoke.set_defaults(handler=_cmd_revoke)

    serve = commands.add_parser("serve", help="run the MCP server")
    serve.add_argument(
        "--stdio", action="store_true", help="serve over stdio using $VAULT_MCP_TOKEN"
    )
    serve.add_argument("--host", default="0.0.0.0", help="HTTP listen address inside the container")  # noqa: S104
    serve.add_argument("--port", type=int, default=8082)
    serve.set_defaults(handler=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or Settings().data_dir
    try:
        store = CredentialStore(data_dir / "tokens.db")
        code: int = args.handler(store, args)
    except (CredentialError, PolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
