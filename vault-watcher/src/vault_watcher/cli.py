"""`vault-watcher` command line (SPEC §7): `run`, `history list|restore|restore-deleted`,
`reindex`. The scripts in `scripts/` run these inside the vault-watcher container."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from vault_core.alerts import AlertSender
from vault_core.config import Settings, bind_addr_warning
from vault_core.fsio import atomic_write
from vault_core.history import HistoryStore, parse_ts
from vault_core.lock import vault_lock
from vault_core.paths import PathError, normalize
from vault_core.policy import Policy, PolicyError, load_policy
from vault_core.search_index import SearchIndex
from vault_watcher.monitor import Monitor, default_checks
from vault_watcher.watcher import Watcher, WatcherConfig

HEALTH_PORT = 8083


class WatcherSettings(Settings):
    history_retention_days: int = 90
    history_min_versions: int = 10
    monitor_interval_s: float = 60.0  # D26
    # ignis-init (D28) and upgrade-vault (D36); paths are inside the container.
    ignis_plugins_file: Path = Path("/kit/ignis-plugins.yaml")
    kit_template_dir: Path = Path("/kit/vault-template")
    ignis_plugin_base_url: str = "https://github.com"
    ignis_plugin_data_mounts: str = ""
    # name=url pairs on the compose network; heartbeats are checked only for enabled services.
    monitor_peers: str = "vault-mcp=http://vault-mcp:8082/health,ignis=http://ignis:8080/"
    monitor_heartbeats: str = "git-sync=git-sync/heartbeat,agent-runner=agent-runner/heartbeat"


def _pairs(raw: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in raw.split(",") if "=" in item)


class CliError(Exception):
    pass


# -- run --------------------------------------------------------------------------------------


def _cmd_run(settings: WatcherSettings, _: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if warning := bind_addr_warning(settings.bind_addr):
        logging.warning(warning)
    config = WatcherConfig(
        vault_dir=settings.vault_dir,
        data_dir=settings.data_dir,
        retention_days=settings.history_retention_days,
        min_versions=settings.history_min_versions,
        health_port=HEALTH_PORT,
        policy=load_policy(settings.policy_file),
    )
    watcher = Watcher(config, alerts=AlertSender(settings.alert_webhook, service="vault-watcher"))

    def _stop(signum: int, _: FrameType | None) -> None:
        logging.info("signal %d: stopping", signum)
        watcher.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    monitor = Monitor(
        default_checks(
            peers=_pairs(settings.monitor_peers),
            heartbeats={
                k: settings.data_dir / v for k, v in _pairs(settings.monitor_heartbeats).items()
            },
            disks=[settings.vault_dir, settings.data_dir],
            heartbeat_max_age_s=3 * settings.monitor_interval_s,
        ),
        AlertSender(settings.alert_webhook, service="vault-watcher"),
    )
    watcher.start()
    monitor.start(settings.monitor_interval_s)
    watcher.wait()
    monitor.stop()
    return 0


# -- history ----------------------------------------------------------------------------------


def _vault_path(raw: str) -> str:
    try:
        return normalize(raw)
    except PathError as exc:
        raise CliError(f"invalid path: {exc}") from None


def _parse_at(text: str) -> datetime:
    """Accept a timestamp as listed by `history list` (20261109T120000.000000Z) or ISO 8601."""
    try:
        return parse_ts(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise CliError(
            f"invalid --at {text!r}: use ISO 8601 (2026-11-09T12:00:00Z) or a listed timestamp"
        ) from None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _write_restored(settings: WatcherSettings, rel: str, content: bytes) -> None:
    """Write through the vault lock, record the restore as a new version, and re-index."""
    target = settings.vault_dir / rel
    ancestor = target.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.resolve().is_relative_to(settings.vault_dir.resolve()):
        raise CliError(f"invalid path: {rel} leaves the vault")
    store = HistoryStore(settings.data_dir)
    with vault_lock(settings.data_dir, "restore"):
        existed = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, content)
        store.record(
            rel, content, event="modify" if existed else "create", source="unknown", kind="restore"
        )
        SearchIndex(settings.data_dir / "index.db", _policy_or_none(settings)).update(rel, content)


def _policy_or_none(settings: WatcherSettings) -> Policy | None:
    try:
        return load_policy(settings.policy_file)
    except PolicyError:
        return None


def _cmd_list(settings: WatcherSettings, args: argparse.Namespace) -> int:
    rel = _vault_path(args.path)
    versions = HistoryStore(settings.data_dir).versions(rel)
    if not versions:
        raise CliError(f"no history for {rel}")
    print(
        f"{'TIMESTAMP (UTC)':<27} {'EVENT':<7} {'SOURCE':<8} {'VERSION':<12} {'AUDIT ID':<26} NOTE"
    )
    for v in versions:
        when = v.time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        note = ", ".join(n for n in (v.kind or "", "unconfirmed" if v.unconfirmed else "") if n)
        print(
            f"{when:<27} {v.event:<7} {v.source:<8} {(v.sha256 or '-')[:12]:<12} {v.audit_id or '-':<26} {note}"
        )
    print(
        f"{len(versions)} versions; restore one with: scripts/history restore {rel} --at <timestamp>",
        file=sys.stderr,
    )
    return 0


def _cmd_restore(settings: WatcherSettings, args: argparse.Namespace) -> int:
    rel = _vault_path(args.path)
    store = HistoryStore(settings.data_dir)
    versions = store.versions(rel)
    if not versions:
        raise CliError(f"no history for {rel}")
    if args.at:
        content = store.content_at(rel, _parse_at(args.at))
        if content is None:
            raise CliError(f"no version of {rel} existed at {args.at} (or it was deleted then)")
    else:
        last = next((v for v in reversed(versions) if v.sha256 is not None), None)
        content = store.content(last) if last else None
        if content is None:
            raise CliError(f"no version of {rel} has content")
    _write_restored(settings, rel, content)
    print(f"restored {rel} ({len(content)} bytes)")
    return 0


def _cmd_restore_deleted(settings: WatcherSettings, args: argparse.Namespace) -> int:
    folder = args.folder.strip("/")
    prefix = _vault_path(folder) + "/" if folder else ""
    deleted = HistoryStore(settings.data_dir).deleted_under(prefix)
    restored = 0
    for rel, content in sorted(deleted.items()):
        if (settings.vault_dir / rel).exists():
            continue
        _write_restored(settings, rel, content)
        print(f"restored {rel}")
        restored += 1
    print(f"{restored} deleted files restored under {prefix or '(vault root)'}", file=sys.stderr)
    return 0


def _cmd_reindex(settings: WatcherSettings, _: argparse.Namespace) -> int:
    index = SearchIndex(settings.data_dir / "index.db", load_policy(settings.policy_file))
    with vault_lock(settings.data_dir, "reindex"):
        count = index.rebuild(settings.vault_dir)
    print(f"indexed {count} documents")
    return 0


# -- entry point ------------------------------------------------------------------------------


def _cmd_harden_ignis(settings: WatcherSettings, args: argparse.Namespace) -> int:
    from vault_watcher import ignis_harden

    try:
        if args.compose:
            print(
                ignis_harden.compose_override(
                    ignis_harden.load_allowlist(settings.ignis_plugins_file)
                ),
                end="",
            )
            return 0
        removed = ignis_harden.harden(
            allowlist=settings.ignis_plugins_file, vault_dir=settings.vault_dir, data_dir=settings.data_dir,
            template_dir=settings.kit_template_dir, base_url=settings.ignis_plugin_base_url,
            mounted=settings.ignis_plugin_data_mounts,
        )  # fmt: skip
    except ignis_harden.HardenError as exc:
        print(f"ignis-init: {exc}", file=sys.stderr)
        return 1
    plugins = [p.id for p in ignis_harden.load_allowlist(settings.ignis_plugins_file)]
    print(
        f"ignis-init: allowlisted plugins: {', '.join(plugins) or 'none'}; removed: {', '.join(removed) or 'nothing'}"
    )
    return 0


def _cmd_upgrade_vault(settings: WatcherSettings, args: argparse.Namespace) -> int:
    from vault_watcher import upgrade

    if not (settings.kit_template_dir / "AGENTS.md").is_file():
        raise CliError(f"no kit template at {settings.kit_template_dir}")
    p = upgrade.plan(settings.vault_dir, settings.kit_template_dir)
    print(upgrade.report(p), end="")
    if not args.apply:
        return 0
    written = upgrade.apply(p, settings.vault_dir, settings.data_dir)
    print(f"upgraded {len(written)} file(s); vault-watcher records their history")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault-watcher", description="Docs Vault Kit watcher")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="watch the vault: version history and search index")
    run.set_defaults(handler=_cmd_run)

    history = commands.add_parser("history", help="list and restore file versions")
    history_cmds = history.add_subparsers(dest="history_command", required=True)
    lister = history_cmds.add_parser("list", help="list versions of a file, oldest first")
    lister.add_argument("path")
    lister.set_defaults(handler=_cmd_list)
    restore = history_cmds.add_parser(
        "restore", help="restore a file (latest content, or as it was --at a time)"
    )
    restore.add_argument("path")
    restore.add_argument("--at", help="UTC time, ISO 8601 or a timestamp from `history list`")
    restore.set_defaults(handler=_cmd_restore)
    deleted = history_cmds.add_parser(
        "restore-deleted", help="restore every deleted file under a folder"
    )
    deleted.add_argument("folder")
    deleted.set_defaults(handler=_cmd_restore_deleted)

    reindex = commands.add_parser("reindex", help="rebuild the search index from the vault")
    reindex.set_defaults(handler=_cmd_reindex)

    upgrade = commands.add_parser("upgrade-vault", help="show (or --apply) kit template updates")
    upgrade.add_argument("--apply", action="store_true", help="write the changes shown")
    upgrade.set_defaults(handler=_cmd_upgrade_vault)

    harden = commands.add_parser(
        "harden-ignis",
        help="ignis-init: enforce the plugin allowlist and managed Obsidian settings",
    )
    harden.add_argument("--compose", action="store_true", help="print compose.plugins.yml instead")
    harden.set_defaults(handler=_cmd_harden_ignis)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.handler(WatcherSettings(), args)
    except (CliError, PolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
