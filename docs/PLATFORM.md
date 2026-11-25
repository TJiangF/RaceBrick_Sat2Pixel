# RaceBrick Cloud Platform

Base URL: `https://racebrick.tfworks.cn`

## Account web

- `/` account SPA (the old satellite debug preview page has been removed).
- Users can register, sign in, bind an ESP by its `device_uid`, and view tracks,
  Race Logs and legacy Race Log summaries.
- Track maps render start line/width/direction. Race Logs open a lap map with
  speed-coloured selected laps, best-lap highlight and per-lap statistics.
- The device page shows the three most recent cloud snapshots with upload size
  and a downloadable JSON copy.
- Admin account is seeded by `RACEBRICK_ADMIN_USER` / `RACEBRICK_ADMIN_PASSWORD`
  (defaults: `admin` / `RaceBrickAdmin#2026`).

## Device provisioning and sync

The server keeps one SQLite database at `data/racebrick.db`.

Device identity is the ESP MAC-derived UID, e.g. `esp32s3-3051EC`. The firmware
must persist a random device secret in NVS and call:

```
POST /api/device/v1/register
{"device_uid":"esp32s3-3051EC","device_secret":"<random>","board":"esp32s3","fw":"..."}
```

Sync is idempotent and transactional:

```
POST /api/device/v1/sync
{
  "device_uid": "esp32s3-3051EC",
  "device_secret": "<random>",
  "since": 17,
  "tracks": [{"local_id":3, "payload":{...}}],
  "sessions": [{"local_slot":5, "payload":{...}, "csv":"Format,3\n..."}],
  "logs": [{"local_index":6, "payload":{...}}],
  "command_acks": []
}
```

- `since` is the last committed server revision stored by the device.
- `sessions[].csv_b64` is accepted as a base64 CSV alternative to `csv`.
- `?nodata=1` on the sync URL commits data but omits echoes of tracks,
  sessions and logs; commands remain available through the dedicated endpoint.
- Every successful sync is stored as a compressed snapshot. The server keeps
  only the newest three snapshots per device; the account page can download them.
- Only after HTTP 200 may the device persist the returned `revision`/`synced_at`.
- A sync body is committed as one transaction; an interrupted upload never
  advances the revision.
- Payload can be omitted when `hash` already matches; server returns
  `need_tracks` / `need_sessions` / `need_logs` for data it still needs.
- The server returns `tracks`, `sessions`, `logs`, `commands`, `revision`,
  `synced_at` and `server_time`.
- Track updates made in the account are queued as `track_update` commands and
  protected from stale device uploads until acknowledged.
- Device should send `command_acks: [id]` only after applying the command and
  saving the track locally.
- Firmware can poll pending track updates as text with
  `GET /api/device/v1/commands?uid=<uid>` and authenticate with the
  `X-Device-Secret` header. Each line is
  `command_id|track_local_id|start_f|start_width|start_offset|reverse`.
- Acknowledge an applied command with
  `POST /api/device/v1/ack?uid=<uid>&id=<command_id>` and the same header.

## Account APIs

All account APIs use `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/platform/register` | register |
| POST | `/api/platform/login` | login |
| GET | `/api/platform/me` | current user |
| POST | `/api/platform/logout` | revoke token |
| GET | `/api/platform/devices` | bound devices |
| POST | `/api/platform/bind` | bind by device UID |
| GET | `/api/platform/devices/{uid}` | tracks/sessions/logs/pending commands |
| GET | `/api/platform/devices/{uid}/sync-history` | newest three sync snapshots |
| GET | `/api/platform/devices/{uid}/sync-history/{id}` | snapshot JSON |
| PATCH | `/api/platform/devices/{uid}/tracks/{local_id}` | update settings and queue device command |
| GET | `/api/platform/admin/users` | admin only |

## Server-side processing

- Track JSON is validated and stored; normalized points are exposed to clients.
- Session CSV is parsed by the server. Lap stats, distances, speed, downsampled
  traces (max 96 points/lap), outlap trimming and the fastest lap are computed
  server-side. Existing records are not modified in flash.
- Old Race Log summaries are stored separately for compatibility.
