"""Core sync engine: fetch torrents, compute diffs, apply changes."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests.exceptions

import qbittorrentapi
from rich.console import Console
from rich.table import Table

from qbittorrent_sync.config import AppConfig, InstanceConfig

log = logging.getLogger("qbt-sync")

_MASTER_TIMEOUT_RECOVERY_SECS = 300


def _is_timeout(exc: BaseException) -> bool:
    """Return True if exc or any of its causes is a requests Timeout."""
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, requests.exceptions.Timeout):
            return True
        e = e.__cause__ or e.__context__
    return False


def _read_torrent_from_disk(bt_backup_path: str, torrent_hash: str) -> bytes | None:
    """Try to read <hash>.torrent directly from the BT_backup directory.

    Returns the raw bytes if found and readable, None otherwise.
    """
    if not bt_backup_path:
        return None
    torrent_file = Path(bt_backup_path) / f"{torrent_hash}.torrent"
    try:
        data = torrent_file.read_bytes()
        if not data:
            log.warning("Empty .torrent file on disk: %s", torrent_file)
            return None
        log.debug("Read .torrent from disk: %s", torrent_file)
        return data
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("Could not read .torrent from disk (%s): %s", torrent_file, e)
        return None


def _master_timeout_recovery() -> None:
    log.warning(
        "Master timed out — pausing %ds to allow recovery before continuing",
        _MASTER_TIMEOUT_RECOVERY_SECS,
    )
    time.sleep(_MASTER_TIMEOUT_RECOVERY_SECS)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TorrentEntry:
    """Lightweight snapshot of a torrent's sync-relevant properties."""

    hash: str
    name: str
    save_path: str
    category: str
    content_path: str
    download_path: str = ""
    tracker: str = ""
    file_priorities: list[int] | None = None


@dataclass
class SyncDiff:
    """Computed diff between master and a single child."""

    child_name: str
    to_delete: list[TorrentEntry] = field(default_factory=list)
    to_add: list[TorrentEntry] = field(default_factory=list)
    to_recategorize: list[tuple[TorrentEntry, TorrentEntry]] = field(default_factory=list)
    to_relocate: list[tuple[TorrentEntry, TorrentEntry]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            not self.to_delete
            and not self.to_add
            and not self.to_recategorize
            and not self.to_relocate
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect(instance: InstanceConfig) -> qbittorrentapi.Client:
    client = qbittorrentapi.Client(
        host=instance.host,
        username=instance.username,
        password=instance.password,
        REQUESTS_ARGS={"timeout": instance.timeout},
    )
    client.auth_log_in()
    log.debug("Connected to %s (%s)", instance.name, instance.host)
    return client


def _torrent_to_entry(t: qbittorrentapi.TorrentDictionary) -> TorrentEntry:
    return TorrentEntry(
        hash=t["hash"],
        name=t.get("name", ""),
        save_path=t.get("save_path", ""),
        category=t.get("category", ""),
        content_path=t.get("content_path", ""),
        download_path=t.get("download_path", ""),
        tracker=t.get("tracker", ""),
    )


def _translate_path(path: str, master_prefix: str, child_prefix: str) -> str:
    """Replace *master_prefix* at the start of *path* with *child_prefix*."""
    if not master_prefix or not child_prefix or not path:
        return path
    norm_mp = master_prefix.rstrip("/")
    norm_path = path.rstrip("/")
    if norm_path == norm_mp or norm_path.startswith(norm_mp + "/"):
        translated = child_prefix.rstrip("/") + norm_path[len(norm_mp):]
        if path.endswith("/") and not translated.endswith("/"):
            translated += "/"
        return translated
    return path


def _translate_entry(
    entry: TorrentEntry, master_prefix: str, child_prefix: str,
) -> TorrentEntry:
    """Return a copy of *entry* with paths translated from master to child space."""
    if not master_prefix or not child_prefix:
        return entry
    return TorrentEntry(
        hash=entry.hash,
        name=entry.name,
        save_path=_translate_path(entry.save_path, master_prefix, child_prefix),
        category=entry.category,
        content_path=_translate_path(entry.content_path, master_prefix, child_prefix),
        download_path=_translate_path(entry.download_path, master_prefix, child_prefix) if entry.download_path else "",
        tracker=entry.tracker,
        file_priorities=entry.file_priorities,
    )


_PAUSED_STATES = {"pausedup", "pauseddl"}


def _fetch_master_torrents(
    client: qbittorrentapi.Client,
    min_seeding_seconds: int,
    *,
    treat_stopped_as_removed: bool = False,
    private_only: bool = True,
    tracker_include: list[re.Pattern[str]] | None = None,
    tracker_exclude: list[re.Pattern[str]] | None = None,
) -> dict[str, TorrentEntry]:
    """Return eligible master torrents keyed by info-hash."""
    torrents = client.torrents_info()

    if tracker_include or tracker_exclude:
        before = len(torrents)
        filtered: list = []
        for t in torrents:
            tracker = t.get("tracker", "")
            if tracker_include and not any(p.search(tracker) for p in tracker_include):
                log.debug("Master tracker filter (include miss): %s [%s]", t.get("name", t["hash"]), tracker)
                continue
            if tracker_exclude and any(p.search(tracker) for p in tracker_exclude):
                log.debug("Master tracker filter (exclude hit): %s [%s]", t.get("name", t["hash"]), tracker)
                continue
            filtered.append(t)
        torrents = filtered
        log.info("Master tracker filter: %d/%d torrent(s) passed", len(torrents), before)

    result: dict[str, TorrentEntry] = {}
    stopped_count = 0
    public_count = 0
    for t in torrents:
        state = (t.get("state") or "").lower()

        if treat_stopped_as_removed and state in _PAUSED_STATES:
            stopped_count += 1
            log.debug("Treating stopped torrent as removed: %s", t.get("name", t["hash"]))
            continue

        if private_only and not t.get("private", False):
            public_count += 1
            log.debug("Skipping public torrent: %s", t.get("name", t["hash"]))
            continue

        is_completed = state in {
            "uploading", "stalledup", "forcedup",
            "pausedup", "queuedup", "checkingup",
            "seeding", "completed",
        }
        if not is_completed and t.get("progress", 0) < 1.0:
            continue

        seeding_time = t.get("seeding_time", 0) or 0
        if seeding_time < min_seeding_seconds:
            log.debug(
                "Skipping %s (seeding %ds < %ds)",
                t.get("name", t["hash"]),
                seeding_time,
                min_seeding_seconds,
            )
            continue

        result[t["hash"]] = _torrent_to_entry(t)

    if treat_stopped_as_removed and stopped_count:
        log.info("Excluded %d stopped/paused torrent(s) from master (treated as removed)", stopped_count)

    if private_only and public_count:
        log.info("Excluded %d public torrent(s) from master", public_count)

    return result


def _fetch_child_torrents(
    client: qbittorrentapi.Client,
) -> dict[str, TorrentEntry]:
    """Return all torrents on a child instance keyed by info-hash."""
    return {t["hash"]: _torrent_to_entry(t) for t in client.torrents_info()}


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def compute_diff(
    master: dict[str, TorrentEntry],
    child: dict[str, TorrentEntry],
    child_name: str,
) -> SyncDiff:
    diff = SyncDiff(child_name=child_name)

    master_hashes = set(master)
    child_hashes = set(child)

    for h in child_hashes - master_hashes:
        diff.to_delete.append(child[h])

    for h in master_hashes - child_hashes:
        diff.to_add.append(master[h])

    for h in master_hashes & child_hashes:
        if master[h].category != child[h].category:
            diff.to_recategorize.append((master[h], child[h]))
        save_differs = master[h].save_path != child[h].save_path
        dl_differs = master[h].download_path != child[h].download_path
        if save_differs or dl_differs:
            diff.to_relocate.append((master[h], child[h]))

    return diff


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _apply_deletes(
    child_client: qbittorrentapi.Client,
    entries: list[TorrentEntry],
) -> int:
    if not entries:
        return 0
    hashes = [e.hash for e in entries]
    try:
        child_client.torrents_delete(delete_files=False, torrent_hashes=hashes)
    except Exception:
        log.warning("Failed to delete %d torrent(s) — skipping", len(entries), exc_info=True)
        return 0
    for e in entries:
        log.info("Deleted torrent: %s", e.name)
    return len(entries)


def _fetch_torrent_bytes(
    master_client: qbittorrentapi.Client,
    entry: TorrentEntry,
    export_timeout: int,
    bt_backup_path: str,
) -> bytes | None:
    """Return raw .torrent bytes from disk cache or master export. Returns None and handles logging/recovery on failure."""
    data = _read_torrent_from_disk(bt_backup_path, entry.hash)
    if data is not None:
        return data
    log.debug("Exporting .torrent from master: %s (%s)", entry.name, entry.hash)
    try:
        return master_client.torrents_export(
            torrent_hash=entry.hash,
            requests_args={"timeout": export_timeout},
        )
    except Exception as e:
        if _is_timeout(e):
            log.warning(
                "Timed out exporting .torrent from master for %s (%s) — skipping",
                entry.name, entry.hash,
            )
            _master_timeout_recovery()
        else:
            log.warning("Failed to export .torrent for %s — skipping", entry.name, exc_info=True)
        return None


def _populate_file_priorities(
    master_client: qbittorrentapi.Client,
    entry: TorrentEntry,
    skip_file_selection: bool,
) -> None:
    """Fetch and cache file priorities on *entry* if not already populated."""
    if skip_file_selection or entry.file_priorities is not None:
        return
    try:
        files = master_client.torrents_files(torrent_hash=entry.hash)
        entry.file_priorities = [f.priority for f in files]
    except Exception:
        log.warning("Failed to fetch file priorities for %s", entry.name, exc_info=True)


def _apply_file_deselections(
    child_client: qbittorrentapi.Client,
    entry: TorrentEntry,
) -> None:
    """Set priority=0 for deselected files then resume the torrent."""
    deselected_ids = [i for i, p in enumerate(entry.file_priorities) if p == 0]
    time.sleep(1)
    try:
        child_client.torrents_file_priority(
            torrent_hash=entry.hash,
            file_ids=deselected_ids,
            priority=0,
        )
        log.debug("Deselected %d file(s) for %s", len(deselected_ids), entry.name)
    except Exception:
        log.warning("Failed to set file priorities for %s", entry.name, exc_info=True)
    try:
        child_client.torrents_resume(torrent_hashes=entry.hash)
    except Exception:
        log.warning("Failed to resume %s after file priority sync", entry.name, exc_info=True)


def _apply_adds(
    master_client: qbittorrentapi.Client,
    child_client: qbittorrentapi.Client,
    entries: list[TorrentEntry],
    skip_hash_check: bool,
    export_timeout: int = 300,
    bt_backup_path: str = "",
    skip_file_selection: bool = False,
) -> int:
    added = 0
    for entry in entries:
        torrent_bytes = _fetch_torrent_bytes(master_client, entry, export_timeout, bt_backup_path)
        if torrent_bytes is None:
            continue

        _populate_file_priorities(master_client, entry, skip_file_selection)

        has_deselected = not skip_file_selection and entry.file_priorities and any(
            p == 0 for p in entry.file_priorities
        )

        add_kwargs: dict = dict(
            torrent_files=torrent_bytes,
            save_path=entry.save_path,
            category=entry.category,
            is_skip_checking=skip_hash_check,
            use_auto_torrent_management=False,
            is_paused=has_deselected,
        )
        if entry.download_path:
            add_kwargs["download_path"] = entry.download_path

        log.debug("Adding torrent: %s → %s", entry.name, entry.save_path)
        try:
            child_client.torrents_add(**add_kwargs)
            if entry.download_path:
                log.info(
                    "Added torrent: %s → %s (temp: %s)",
                    entry.name, entry.save_path, entry.download_path,
                )
            else:
                log.info("Added torrent: %s → %s", entry.name, entry.save_path)
            added += 1
        except qbittorrentapi.Conflict409Error:
            log.debug("Torrent already exists on child: %s", entry.name)
            continue
        except Exception:
            log.warning("Failed to add torrent %s — skipping", entry.name, exc_info=True)
            continue

        if has_deselected:
            _apply_file_deselections(child_client, entry)

    return added


def _apply_recategorize(
    child_client: qbittorrentapi.Client,
    entries: list[tuple[TorrentEntry, TorrentEntry]],
) -> int:
    recategorized = 0
    for master_entry, child_entry in entries:
        try:
            child_client.torrents_set_category(
                category=master_entry.category,
                torrent_hashes=master_entry.hash,
            )
            log.info(
                "Recategorized torrent: %s (%r → %r)",
                master_entry.name,
                child_entry.category,
                master_entry.category,
            )
            recategorized += 1
        except Exception:
            log.warning(
                "Failed to recategorize torrent %s — skipping",
                master_entry.name,
                exc_info=True,
            )
    return recategorized


def _apply_relocates(
    child_client: qbittorrentapi.Client,
    entries: list[tuple[TorrentEntry, TorrentEntry]],
) -> int:
    relocated = 0
    for master_entry, child_entry in entries:
        h = master_entry.hash
        try:
            child_client.torrents_pause(torrent_hashes=h)

            if master_entry.save_path != child_entry.save_path:
                child_client.torrents_set_save_path(
                    save_path=master_entry.save_path,
                    torrent_hashes=h,
                )

            if master_entry.download_path != child_entry.download_path:
                child_client.torrents_set_download_path(
                    download_path=master_entry.download_path,
                    torrent_hashes=h,
                )

            child_client.torrents_resume(torrent_hashes=h)

            parts = []
            if master_entry.save_path != child_entry.save_path:
                parts.append(f"save_path: {child_entry.save_path} → {master_entry.save_path}")
            if master_entry.download_path != child_entry.download_path:
                parts.append(f"temp_path: {child_entry.download_path!r} → {master_entry.download_path!r}")
            log.info("Relocated torrent: %s (%s)", master_entry.name, "; ".join(parts))
            relocated += 1
        except Exception:
            log.warning("Failed to relocate torrent %s — skipping", master_entry.name, exc_info=True)
    return relocated


def _apply_relocates_by_readd(
    master_client: qbittorrentapi.Client,
    child_client: qbittorrentapi.Client,
    entries: list[tuple[TorrentEntry, TorrentEntry]],
    skip_hash_check: bool,
    export_timeout: int = 300,
    bt_backup_path: str = "",
    skip_file_selection: bool = False,
) -> int:
    """Delete and re-add torrents to update their save path without moving files.

    Used when the child mounts storage read-only, so setSavePath would return 403.
    """
    readded = 0
    for master_entry, child_entry in entries:
        h = master_entry.hash

        torrent_bytes = _fetch_torrent_bytes(master_client, master_entry, export_timeout, bt_backup_path)
        if torrent_bytes is None:
            continue

        _populate_file_priorities(master_client, master_entry, skip_file_selection)

        has_deselected = not skip_file_selection and master_entry.file_priorities and any(
            p == 0 for p in master_entry.file_priorities
        )

        try:
            child_client.torrents_delete(delete_files=False, torrent_hashes=h)
        except Exception:
            log.warning("Failed to delete %s before re-add — skipping", master_entry.name, exc_info=True)
            continue

        add_kwargs: dict = dict(
            torrent_files=torrent_bytes,
            save_path=master_entry.save_path,
            category=master_entry.category,
            is_skip_checking=skip_hash_check,
            use_auto_torrent_management=False,
            is_paused=has_deselected,
        )
        if master_entry.download_path:
            add_kwargs["download_path"] = master_entry.download_path

        try:
            child_client.torrents_add(**add_kwargs)
        except Exception:
            log.warning("Failed to re-add %s — skipping", master_entry.name, exc_info=True)
            continue

        if has_deselected:
            _apply_file_deselections(child_client, master_entry)

        parts = []
        if master_entry.save_path != child_entry.save_path:
            parts.append(f"save_path: {child_entry.save_path} → {master_entry.save_path}")
        if master_entry.download_path != child_entry.download_path:
            parts.append(f"temp_path: {child_entry.download_path!r} → {master_entry.download_path!r}")
        log.info("Re-added torrent (path update): %s (%s)", master_entry.name, "; ".join(parts))
        readded += 1

    return readded



# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def _print_diff_table(diff: SyncDiff, console: Console, dry_run: bool) -> None:
    label = "[bold yellow][DRY RUN][/] " if dry_run else ""
    title = f"{label}Sync summary for [bold cyan]{diff.child_name}[/]"

    table = Table(title=title, show_lines=True)
    table.add_column("Action", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Details")

    if diff.to_delete:
        names = "\n".join(e.name for e in diff.to_delete[:10])
        if len(diff.to_delete) > 10:
            names += f"\n… and {len(diff.to_delete) - 10} more"
        table.add_row("[red]Delete[/]", str(len(diff.to_delete)), names)

    if diff.to_add:
        names = "\n".join(e.name for e in diff.to_add[:10])
        if len(diff.to_add) > 10:
            names += f"\n… and {len(diff.to_add) - 10} more"
        table.add_row("[green]Add[/]", str(len(diff.to_add)), names)

    if diff.to_recategorize:
        details: list[str] = []
        for master_e, child_e in diff.to_recategorize[:10]:
            details.append(f"{master_e.name}: {child_e.category!r} → {master_e.category!r}")
        if len(diff.to_recategorize) > 10:
            details.append(f"… and {len(diff.to_recategorize) - 10} more")
        table.add_row("[cyan]Recategorize[/]", str(len(diff.to_recategorize)), "\n".join(details))

    if diff.to_relocate:
        details = []
        for master_e, child_e in diff.to_relocate[:10]:
            parts: list[str] = []
            if master_e.save_path != child_e.save_path:
                parts.append(f"{child_e.save_path} → {master_e.save_path}")
            if master_e.download_path != child_e.download_path:
                parts.append(f"temp: {child_e.download_path or '(none)'} → {master_e.download_path or '(none)'}")
            details.append(f"{master_e.name}: {'; '.join(parts)}")
        if len(diff.to_relocate) > 10:
            details.append(f"… and {len(diff.to_relocate) - 10} more")
        table.add_row("[yellow]Relocate[/]", str(len(diff.to_relocate)), "\n".join(details))

    if diff.is_empty:
        table.add_row("[dim]—[/]", "0", "Already in sync")

    console.print(table)


# ---------------------------------------------------------------------------
# Pre-sync cleanup
# ---------------------------------------------------------------------------

_STALE_STATES = {"error", "missingfiles"}


def _cleanup_stale_torrents(
    client: qbittorrentapi.Client,
    child_name: str,
    console: Console,
    dry_run: bool,
) -> int:
    """Remove errored or 0-progress torrents from a child before sync."""
    torrents = client.torrents_info()
    to_remove: list[tuple[str, str, str]] = []

    for t in torrents:
        state = (t.get("state") or "").lower()
        name = t.get("name", t["hash"])

        if state in _STALE_STATES:
            to_remove.append((t["hash"], name, f"state={state}"))
            continue

        progress = t.get("progress", 0) or 0
        if progress <= 0:
            to_remove.append((t["hash"], name, "0% progress"))

    if not to_remove:
        console.print("  No stale torrents found.")
        return 0

    table = Table(
        title=f"Stale torrents on [bold cyan]{child_name}[/]",
        show_lines=False,
    )
    table.add_column("Torrent", style="white")
    table.add_column("Reason", style="yellow")
    for _, name, reason in to_remove:
        table.add_row(name, reason)
    console.print(table)

    if dry_run:
        console.print(
            f"  [bold yellow][DRY RUN][/] Would remove {len(to_remove)} stale torrent(s).\n"
        )
        return 0

    hashes = [h for h, _, _ in to_remove]
    client.torrents_delete(delete_files=False, torrent_hashes=hashes)
    log.info("Removed %d stale torrent(s) from %s", len(to_remove), child_name)
    console.print(
        f"  Removed [bold red]{len(to_remove)}[/] stale torrent(s).\n"
    )
    return len(to_remove)


# ---------------------------------------------------------------------------
# Per-child tracker filtering
# ---------------------------------------------------------------------------

def _filter_by_tracker(
    master: dict[str, TorrentEntry],
    include: list[re.Pattern[str]],
    exclude: list[re.Pattern[str]],
) -> dict[str, TorrentEntry]:
    """Return the subset of *master* whose tracker URL matches the child's rules.

    - *include*: if non-empty, the tracker must match at least one pattern.
    - *exclude*: if non-empty, the tracker must NOT match any pattern.
    Include is checked first; exclude is applied on the surviving set.
    """
    if not include and not exclude:
        return master

    filtered: dict[str, TorrentEntry] = {}
    skipped = 0
    for h, entry in master.items():
        tracker = entry.tracker

        if include and not any(p.search(tracker) for p in include):
            skipped += 1
            log.debug("Tracker filter (include miss): %s [%s]", entry.name, tracker)
            continue

        if exclude and any(p.search(tracker) for p in exclude):
            skipped += 1
            log.debug("Tracker filter (exclude hit): %s [%s]", entry.name, tracker)
            continue

        filtered[h] = entry

    if skipped:
        log.info("Tracker filter excluded %d torrent(s) for this child", skipped)
    return filtered


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_sync(cfg: AppConfig, *, dry_run: bool, console: Console) -> None:
    """Execute a full sync cycle."""
    min_seed_secs = cfg.sync.min_seeding_time_minutes * 60

    # --- pre-sync: clean stale torrents from children ---
    console.print("\n[bold]Pre-sync cleanup[/]: scanning children for stale torrents …\n")
    for child_cfg in cfg.children:
        console.rule(f"[dim]{child_cfg.name}[/] — {child_cfg.host}")
        try:
            child_client = _connect(child_cfg)
        except Exception as e:
            if _is_timeout(e):
                log.error("Timed out connecting to child %s at %s — skipping cleanup", child_cfg.name, child_cfg.host)
            else:
                log.error("Cannot connect to child %s at %s — skipping cleanup", child_cfg.name, child_cfg.host, exc_info=True)
            continue
        try:
            _cleanup_stale_torrents(child_client, child_cfg.name, console, dry_run)
        except Exception:
            log.error("Cleanup failed for child %s — skipping", child_cfg.name, exc_info=True)

    # --- master ---
    console.print(f"\nConnecting to master [bold]{cfg.master.host}[/] …")
    try:
        master_client = _connect(cfg.master)
    except Exception as e:
        if _is_timeout(e):
            log.error("Timed out connecting to master at %s", cfg.master.host)
        else:
            log.exception("Cannot connect to master at %s", cfg.master.host)
        raise

    skip_file_selection = cfg.sync.skip_file_selection
    treat_stopped = cfg.sync.treat_stopped_as_removed
    private_only = cfg.sync.private_only
    master_torrents = _fetch_master_torrents(
        master_client, min_seed_secs,
        treat_stopped_as_removed=treat_stopped,
        private_only=private_only,
        tracker_include=cfg.master.tracker_include or None,
        tracker_exclude=cfg.master.tracker_exclude or None,
    )
    console.print(f"  Found [bold]{len(master_torrents)}[/] eligible torrent(s) on master.")
    if cfg.master.tracker_include or cfg.master.tracker_exclude:
        console.print("  [dim]Master tracker filter is active.[/]")
    if private_only:
        console.print("  [dim]Only private torrents are being synced (private_only is enabled).[/]")
    if treat_stopped:
        console.print("  [dim]Stopped/paused torrents on master are treated as removed.[/]")
    console.print()

    # --- children ---
    for child_cfg in cfg.children:
        console.rule(f"[bold]{child_cfg.name}[/] — {child_cfg.host}")
        try:
            child_client = _connect(child_cfg)
        except Exception as e:
            if _is_timeout(e):
                log.error("Timed out connecting to child %s at %s — skipping", child_cfg.name, child_cfg.host)
            else:
                log.error("Cannot connect to child %s at %s — skipping", child_cfg.name, child_cfg.host, exc_info=True)
            continue

        try:
            child_torrents = _fetch_child_torrents(child_client)
        except Exception as e:
            if _is_timeout(e):
                log.error("Timed out fetching torrent list from child %s — skipping", child_cfg.name)
            else:
                log.error("Failed to fetch torrent list from child %s — skipping", child_cfg.name, exc_info=True)
            continue
        log.debug("Child %s has %d torrent(s)", child_cfg.name, len(child_torrents))

        master_path = cfg.master.path
        child_path = child_cfg.path
        if master_path and child_path:
            translated_master = {
                h: _translate_entry(e, master_path, child_path)
                for h, e in master_torrents.items()
            }
            console.print(f"  [dim]Path translation: {master_path} → {child_path}[/]")
        else:
            translated_master = master_torrents

        if child_cfg.tracker_include or child_cfg.tracker_exclude:
            before = len(translated_master)
            translated_master = _filter_by_tracker(
                translated_master, child_cfg.tracker_include, child_cfg.tracker_exclude,
            )
            console.print(
                f"  [dim]Tracker filter: {len(translated_master)}/{before} torrent(s) matched[/]"
            )

        diff = compute_diff(translated_master, child_torrents, child_cfg.name)
        _print_diff_table(diff, console, dry_run)

        if dry_run or diff.is_empty:
            continue

        deleted = _apply_deletes(child_client, diff.to_delete)
        added = _apply_adds(master_client, child_client, diff.to_add, cfg.sync.skip_hash_check, export_timeout=cfg.master.timeout * 5, bt_backup_path=cfg.bt_backup_path, skip_file_selection=skip_file_selection)
        recategorized = _apply_recategorize(child_client, diff.to_recategorize)
        if child_cfg.readd_on_relocate:
            relocated = _apply_relocates_by_readd(master_client, child_client, diff.to_relocate, cfg.sync.skip_hash_check, export_timeout=cfg.master.timeout * 5, bt_backup_path=cfg.bt_backup_path, skip_file_selection=skip_file_selection)
        else:
            relocated = _apply_relocates(child_client, diff.to_relocate)

        console.print(
            f"\n  [bold green]Done:[/] {deleted} deleted, {added} added,"
            f" {recategorized} recategorized, {relocated} relocated.\n"
        )
