# Docker test stack

Compose file: `compose.yaml` (paths below are relative to `test/` unless noted).

## How it works

`qbt-master`, `qbt-child`, and `qbt-sync` attach only to `internal_network` with `internal: true`, so they have no route to the public internet. On many Docker setups, `ports:` on an internal-only service does not map to the host (empty `docker port` output), so the qBittorrent Web UI is not published from those containers directly.

`webui-proxy` (nginx) joins `internal_network` and a normal `edge` bridge. Only the proxy publishes **127.0.0.1:9080** → master Web UI and **127.0.0.1:9081** → child Web UI. Your browser talks to nginx; nginx talks to qBittorrent on the internal network. qBittorrent still has a single NIC on the internal network only.

The proxy also has `edge` and could reach the internet if something inside tried; the image is stock nginx with a static config (upstream is only qBittorrent). To bind the Web UI on all interfaces, change `ports` in `compose.yaml` from `127.0.0.1:9080:80` to `9080:80` (and the same pattern for `9081`).

## Layout

| Host path     | qbt-master / qbt-child           | qbt-sync                           |
| ------------- | -------------------------------- | ---------------------------------- |
| `qbt-master/` | `/config` (read-write)           | `/qbt-master-ro` (read-only)       |
| `qbt-child/`  | `/config` (read-write)           | —                                  |
| `downloads/`  | `/downloads` (read-only, shared) | —                                  |
| `qbt-sync/`   | —                                | `config.yaml` → `/app/config.yaml` |

Both qBittorrent instances share `test/downloads` mounted at `/downloads` as **read-only** (matches the default save path from `qBittorrent.conf`). New downloads or temp files under `/downloads` will fail on disk; use this stack for sync/API behaviour, or change the mount to `:rw` when you need real writes.

`qbt_data_path` in `qbt-sync/config.yaml` must point at the directory that contains `torrents.db` / `BT_backup` on the mounted master data tree (see `compose.yaml`: volume → `/qbt-data` in the container).

## Setting Up

The `qbt-sync` service in `compose.yaml` runs in **daemon** mode with `--debug-sync-anything`, so every master torrent is in the sync set. Per-child `tracker_include` / `tracker_exclude` still apply.

After changing application code, rebuild the image:

```bash
docker compose -f test/compose.yaml build qbt-sync
docker compose -f test/compose.yaml up -d --force-recreate
```

Web UIs (change passwords after first login):

- Master: [http://127.0.0.1:9080](http://127.0.0.1:9080) — nginx → `qbt-master:9080` on the internal network.
- Child: [http://127.0.0.1:9081](http://127.0.0.1:9081) — nginx → `qbt-child:9081`.

If `admin` / `adminadmin` does not work, check container logs for a temporary password, then update `qbt-sync/config.yaml`.
