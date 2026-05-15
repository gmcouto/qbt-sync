# qbt-sync

Synchronize torrents from a master qBittorrent instance to one or more child instances. Handles adding, deleting, relocating torrents, and syncing file selections.

Designed for setups where multiple qBittorrent instances share the same storage (e.g. via NFS/SMB mounts).

## Install

Create a `config.yaml` (see `config.example.yaml`) and run:

```bash
docker run -d --restart unless-stopped \
  -v ./config.yaml:/app/config.yaml:ro \
  --name qbt-sync \
  ghcr.io/gmcouto/qbt-sync:latest
```

The container runs in daemon mode by default, syncing every N minutes as configured in `daemon_run_interval_minutes`.

## What it does

1. Cleans up stale/errored torrents on children
2. Fetches eligible torrents from master (completed + minimum seeding time)
3. For each child, computes a diff and applies:
   - **Delete** torrents not on master
   - **Add** torrents missing on child
   - **Recategorize** torrents whose category differs from master
   - **Relocate** torrents with mismatched save paths or temp (download) paths
   - **Sync file selections** (deselected files on master get deselected on children)
