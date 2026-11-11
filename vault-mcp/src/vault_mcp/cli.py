"""`vault-mcp` command line (SPEC §6.3): `token create|list|revoke`, `oauth approve-code`, `serve`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vault_core.audit import AuditLog
from vault_core.config import Settings, bind_addr_warning
from vault_core.policy import PolicyError
from vault_mcp.store import (
    DEFAULT_EXPIRY,
    OWNER_RE,
    Credential,
    CredentialError,
    CredentialStore,
    parse_expiry,
)


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


def _audit(store: CredentialStore, tool: str, **fields: Any) -> None:
    """Admin actions go into the same audit log as tool calls (service `vault-mcp-cli`)."""
    AuditLog(store.path.parent / "audit.log").append(
        service="vault-mcp-cli", result="ok", tool=tool, **fields
    )


def _cmd_create(store: CredentialStore, args: argparse.Namespace) -> int:
    folders = [f.strip() for f in args.folders.split(",") if f.strip()] if args.folders else None
    token, cred = store.create_bearer(
        name=args.name, owner=args.owner, scope=args.scope, folders=folders, expires=args.expires
    )
    print(token)
    _audit(store, "token_create", credential_id=cred.id, credential_name=cred.name, owner=cred.owner,
           scope=cred.scope, folders=cred.folders)  # fmt: skip
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
    _audit(
        store, "token_revoke", credential_id=cred.id, credential_name=cred.name, owner=cred.owner
    )
    print(f"revoked {cred.name} ({cred.id})", file=sys.stderr)
    return 0


def _cmd_serve(store: CredentialStore, args: argparse.Namespace) -> int:
    from vault_mcp.server import build_server

    settings = Settings() if args.data_dir is None else Settings(data_dir=args.data_dir)
    if warning := bind_addr_warning(settings.bind_addr):
        print(f"WARNING: {warning}", file=sys.stderr)
    if settings.mcp_oauth_enabled and not settings.mcp_oauth_issuer and not args.stdio:
        print(
            "WARNING: MCP_OAUTH_ENABLED is true but MCP_OAUTH_ISSUER is empty: OAuth is off, bearer tokens only. "
            "Set MCP_OAUTH_ISSUER to the public https URL of the MCP host to let Claude.ai and ChatGPT connect.",
            file=sys.stderr,
        )
    if args.stdio:
        token = os.environ.get("VAULT_MCP_TOKEN", "")
        if store.verify(token) is None:
            print(
                "error: VAULT_MCP_TOKEN is missing, invalid, expired, or revoked", file=sys.stderr
            )
            return 2
        build_server(settings, stdio_token=token).run(transport="stdio", show_banner=False)
    else:
        try:
            server = build_server(settings)
        except ValueError as exc:  # invalid OAUTH_UPSTREAM_* settings: refuse to start
            print(f"error: {exc}", file=sys.stderr)
            return 2
        server.run(transport="http", host=args.host, port=args.port, path="/mcp", show_banner=False)
    return 0


def _cmd_approve_code(store: CredentialStore, args: argparse.Namespace) -> int:
    from vault_mcp.oauth.grants import OAuthStore

    if not OWNER_RE.match(args.owner):
        raise CredentialError(f"invalid owner {args.owner!r}: expected an email address")
    now = store.now()
    expires = parse_expiry(args.ttl, now) if args.ttl != "never" else None
    if expires is None or expires - now > timedelta(days=7):
        raise CredentialError("--ttl must be between 1 minute and 7 days")
    code = OAuthStore(store).create_approval_code(args.owner, args.scope, expires - now)
    print(code)
    _audit(
        store,
        "oauth_approve_code",
        owner=args.owner,
        scope=args.scope,
        expires_at=expires.isoformat(),
    )
    print(
        f"one-time code for {args.owner} ({args.scope}), valid until {_fmt(expires)}; "
        "they enter it on the sign-in page when connecting an app",
        file=sys.stderr,
    )
    return 0


def _cmd_revoke_codes(store: CredentialStore, args: argparse.Namespace) -> int:
    from vault_mcp.oauth.grants import OAuthStore

    count = OAuthStore(store).revoke_approval_codes(args.owner)
    _audit(store, "oauth_revoke_codes", owner=args.owner, count=count)
    print(f"cancelled {count} unused approval code(s) for {args.owner}", file=sys.stderr)
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

    oauth = commands.add_parser("oauth", help="OAuth sign-in for hosted MCP clients")
    oauth_cmds = oauth.add_subparsers(dest="oauth_command", required=True)
    approve = oauth_cmds.add_parser(
        "approve-code",
        help="one-time code a person enters to connect an app (no identity provider)",
    )
    approve.add_argument("--owner", required=True, help="email of the person who will sign in")
    approve.add_argument(
        "--scope",
        choices=["read", "write"],
        default="read",
        help="most the person may grant (default read)",
    )
    approve.add_argument(
        "--ttl", default="1h", help="<n>m|<n>h|<n>d until the code expires (default 1h)"
    )
    approve.set_defaults(handler=_cmd_approve_code)
    revoke_codes = oauth_cmds.add_parser(
        "revoke-codes", help="cancel every unused approval code for a person (offboarding)"
    )
    revoke_codes.add_argument("--owner", required=True)
    revoke_codes.set_defaults(handler=_cmd_revoke_codes)

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
