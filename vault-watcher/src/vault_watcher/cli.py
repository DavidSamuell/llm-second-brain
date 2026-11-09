"""`vault-watcher` command line. `run` starts the service (SPEC §7)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Sequence
from types import FrameType

from vault_core.alerts import AlertSender
from vault_core.config import Settings, bind_addr_warning
from vault_core.policy import load_policy
from vault_watcher.watcher import Watcher, WatcherConfig

HEALTH_PORT = 8083


class WatcherSettings(Settings):
    history_retention_days: int = 90
    history_min_versions: int = 10
    alert_webhook: str | None = None


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
    watcher.start()
    watcher.wait()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault-watcher", description="Docs Vault Kit watcher")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="watch the vault: version history (and, later, index and secret scan)"
    )
    run.set_defaults(handler=_cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.handler(WatcherSettings(), args)
    return code


if __name__ == "__main__":
    sys.exit(main())
