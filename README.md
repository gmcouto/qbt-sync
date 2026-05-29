# qbt-sync

Synchronize torrents from a master qBittorrent instance to one or more child instances. Handles adding, deleting, relocating torrents.

Designed for setups where multiple qBittorrent instances share the same storage (e.g. via NFS/SMB mounts).

## Install

Create a `config.yaml` (see `config.example.yaml`) and run:

```bash
docker run -d --restart unless-stopped \
  -v ./config.yaml:/app/config.yaml:ro \
  -v /optional/path/to/master/qbittorrent/BT_backup:/BT_backup:ro \
  --name qbt-sync \
  ghcr.io/gmcouto/qbt-sync:latest
```

The container runs in daemon mode by default, syncing every N minutes as configured in `daemon_run_interval_minutes`.
`BT_backup` volume is optional, used to skip `.torrent` export api, that can cause hanging in some specific scenarios (like a whole library of thousands of books in a single `.torrent` file)

## What it does

1. Cleans up stale/errored torrents on children
2. Fetches eligible torrents from master (completed + minimum seeding time)
3. For each child, computes a diff and applies:
   - **Delete** torrents not on master
   - **Add** torrents missing on child
   - **Recategorize** torrents whose category differs from master
   - **Relocate** torrents with mismatched save paths or temp (download) paths
