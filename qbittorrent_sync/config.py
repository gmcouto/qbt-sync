"""YAML configuration loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class InstanceConfig:
    """Connection details for a qBittorrent instance."""

    host: str
    username: str
    password: str
    name: str = ""
    path: str = ""
    readd_on_relocate: bool = False
    tracker_include: list[re.Pattern[str]] = field(default_factory=list)
    tracker_exclude: list[re.Pattern[str]] = field(default_factory=list)


@dataclass
class SyncConfig:
    """Tuning knobs for the sync behaviour."""

    min_seeding_time_minutes: int = 10
    skip_hash_check: bool = True
    dry_run: bool = True
    private_only: bool = True
    sync_file_selections: bool = False
    treat_stopped_as_removed: bool = False
    daemon_run_interval_minutes: int = 15


@dataclass
class AppConfig:
    """Top-level application configuration."""

    master: InstanceConfig
    children: list[InstanceConfig]
    sync: SyncConfig = field(default_factory=SyncConfig)


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""


def _compile_patterns(raw: list | None, field_name: str, instance_name: str) -> list[re.Pattern[str]]:
    if not raw:
        return []
    patterns: list[re.Pattern[str]] = []
    for item in raw:
        try:
            patterns.append(re.compile(item))
        except re.error as exc:
            raise ConfigError(
                f"Invalid regex in {field_name} for {instance_name}: {item!r} ({exc})"
            )
    return patterns


def _parse_instance(data: dict, default_name: str = "") -> InstanceConfig:
    missing = [k for k in ("host", "username", "password") if k not in data]
    if missing:
        raise ConfigError(f"Instance config missing required fields: {', '.join(missing)}")
    name = data.get("name", default_name)
    return InstanceConfig(
        host=data["host"],
        username=data["username"],
        password=data["password"],
        name=name,
        path=data.get("path", ""),
        readd_on_relocate=bool(data.get("readd_on_relocate", False)),
        tracker_include=_compile_patterns(data.get("tracker_include"), "tracker_include", name),
        tracker_exclude=_compile_patterns(data.get("tracker_exclude"), "tracker_exclude", name),
    )


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a YAML config file, returning an ``AppConfig``."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path) as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigError("Config file must be a YAML mapping")

    if "master" not in raw:
        raise ConfigError("Config must contain a 'master' section")
    master = _parse_instance(raw["master"], default_name="master")

    if "children" not in raw or not raw["children"]:
        raise ConfigError("Config must contain a non-empty 'children' list")
    children = [
        _parse_instance(child, default_name=f"child-{i}")
        for i, child in enumerate(raw["children"], start=1)
    ]

    sync_raw = raw.get("sync", {})
    sync = SyncConfig(
        min_seeding_time_minutes=int(sync_raw.get("min_seeding_time_minutes", 10)),
        skip_hash_check=bool(sync_raw.get("skip_hash_check", True)),
        dry_run=bool(sync_raw.get("dry_run", True)),
        private_only=bool(sync_raw.get("private_only", True)),
        sync_file_selections=bool(sync_raw.get("sync_file_selections", False)),
        treat_stopped_as_removed=bool(sync_raw.get("treat_stopped_as_removed", False)),
        daemon_run_interval_minutes=int(sync_raw.get("daemon_run_interval_minutes", 15)),
    )

    return AppConfig(master=master, children=children, sync=sync)
