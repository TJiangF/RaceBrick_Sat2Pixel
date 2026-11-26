"""RaceBrick cloud platform: accounts, device binding, device sync, track/race data.

This module intentionally uses only the Python standard library plus FastAPI so
the existing Sat2Pixel service can be deployed without extra infrastructure.
The database is SQLite and is safe for a single uvicorn worker.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RACEBRICK_DB", ROOT / "data" / "racebrick.db"))
ADMIN_USER = os.environ.get("RACEBRICK_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("RACEBRICK_ADMIN_PASSWORD", "RaceBrickAdmin#2026")
TOKEN_TTL = 60 * 60 * 24 * 30
ALLOW_DIRECT_DEVICE = os.getenv("RACEBRICK_ALLOW_DIRECT_DEVICE", "0") == "1"
DEVICE_RE = re.compile(r"^[A-Za-z0-9._:-]{3,64}$")

router = APIRouter()


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 220_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def _check_password(password: str, encoded: str) -> bool:
    try:
        salt_b64, digest_b64 = encoded.split("$", 1)
        salt = base64.b64decode(salt_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 220_000)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(("device:" + secret).encode()).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(payload: Any, csv_text: str | None = None) -> str:
    h = hashlib.sha256(_canonical(payload).encode())
    if csv_text:
        h.update(b"\n--csv--\n")
        h.update(csv_text.encode())
    return h.hexdigest()


def _init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                uid TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                board TEXT NOT NULL DEFAULT '',
                fw TEXT NOT NULL DEFAULT '',
                secret_hash TEXT,
                owner_id INTEGER,
                created_at INTEGER NOT NULL,
                last_seen INTEGER,
                last_sync_at INTEGER,
                last_sync_revision INTEGER NOT NULL DEFAULT 0,
                data_version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                local_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                track_uuid TEXT,
                revision_id TEXT,
                parent_revision_id TEXT,
                track_version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER,
                UNIQUE(device_uid, local_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                local_slot INTEGER NOT NULL,
                payload TEXT NOT NULL,
                csv TEXT,
                summary TEXT,
                content_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                track_local_id INTEGER,
                track_name TEXT NOT NULL DEFAULT '',
                start_epoch INTEGER,
                duration_ms INTEGER,
                lap_count INTEGER NOT NULL DEFAULT 0,
                best_lap INTEGER,
                best_time REAL,
                session_uuid TEXT,
                track_uuid TEXT,
                track_revision_id TEXT,
                track_version INTEGER NOT NULL DEFAULT 0,
                end_epoch INTEGER,
                sample_size INTEGER NOT NULL DEFAULT 16,
                raw_format INTEGER NOT NULL DEFAULT 16,
                metadata_hash TEXT,
                laps_hash TEXT,
                telemetry_hash TEXT,
                UNIQUE(device_uid, local_slot)
            );
            CREATE TABLE IF NOT EXISTS race_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                local_index INTEGER NOT NULL,
                payload TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                track_name TEXT NOT NULL DEFAULT '',
                lap_count INTEGER NOT NULL DEFAULT 0,
                best_lap INTEGER,
                best_time REAL,
                UNIQUE(device_uid, local_index)
            );
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                ack_at INTEGER,
                result TEXT
            );
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                upload_bytes INTEGER NOT NULL,
                track_count INTEGER NOT NULL,
                session_count INTEGER NOT NULL,
                log_count INTEGER NOT NULL,
                payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS track_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                track_uuid TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                parent_revision_id TEXT,
                payload TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT 'device',
                created_at INTEGER NOT NULL,
                UNIQUE(device_uid, track_uuid, revision_id)
            );
            CREATE TABLE IF NOT EXISTS lap_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                session_uuid TEXT NOT NULL,
                lap_id TEXT NOT NULL,
                lap_number INTEGER NOT NULL,
                lap_time_ms INTEGER NOT NULL,
                sector_times_ms TEXT NOT NULL DEFAULT '[]',
                start_time INTEGER,
                end_time INTEGER,
                best_lap INTEGER NOT NULL DEFAULT 0,
                telemetry_reference TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                UNIQUE(device_uid, session_uuid, lap_id)
            );
            CREATE TABLE IF NOT EXISTS telemetry_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_uid TEXT NOT NULL,
                session_uuid TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL DEFAULT 0,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT,
                created_at INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(device_uid, chunk_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tracks_dev_rev ON tracks(device_uid, revision);
            CREATE INDEX IF NOT EXISTS idx_sessions_dev_rev ON sessions(device_uid, revision);
            CREATE INDEX IF NOT EXISTS idx_logs_dev_rev ON race_logs(device_uid, revision);
            CREATE INDEX IF NOT EXISTS idx_commands_dev ON commands(device_uid, status);
            CREATE INDEX IF NOT EXISTS idx_sync_history_dev ON sync_history(device_uid, id);
            CREATE INDEX IF NOT EXISTS idx_tokens_exp ON auth_tokens(expires_at);
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
        if "data_version" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN data_version INTEGER NOT NULL DEFAULT 0")

        def ensure_cols(table: str, defs: dict[str, str]) -> None:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in defs.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

        ensure_cols("tracks", {
            "track_uuid": "TEXT", "revision_id": "TEXT",
            "parent_revision_id": "TEXT", "track_version": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "INTEGER",
        })
        ensure_cols("sessions", {
            "session_uuid": "TEXT", "track_uuid": "TEXT", "track_revision_id": "TEXT",
            "track_version": "INTEGER NOT NULL DEFAULT 0", "end_epoch": "INTEGER",
            "sample_size": "INTEGER NOT NULL DEFAULT 16",
            "raw_format": "INTEGER NOT NULL DEFAULT 16",
            "metadata_hash": "TEXT", "laps_hash": "TEXT", "telemetry_hash": "TEXT",
        })
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_uuid ON tracks(device_uid, track_uuid) WHERE track_uuid IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(device_uid, session_uuid) WHERE session_uuid IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_laps_session ON lap_records(device_uid, session_uuid, lap_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_session ON telemetry_chunks(device_uid, session_uuid, seq)")
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('revision','0')")
        row = conn.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                (ADMIN_USER, _hash_password(ADMIN_PASSWORD), "admin", _now()),
            )


def _revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
    return int(row["value"]) if row else 0


def _next_revision(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    return _revision(conn)


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

def _user_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "login required")
    token = authorization[7:].strip()
    with db() as conn:
        row = conn.execute(
            """SELECT u.* FROM auth_tokens t JOIN users u ON u.id=t.user_id
               WHERE t.token_hash=? AND t.expires_at>?""",
            (_token_hash(token), _now()),
        ).fetchone()
        if not row:
            raise HTTPException(401, "invalid or expired token")
        return _user_dict(row)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "admin only")
    return user


def _make_token(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_tokens(token_hash,user_id,expires_at) VALUES(?,?,?)",
        (_token_hash(token), user_id, _now() + TOKEN_TTL),
    )
    return token


def _get_device(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM devices WHERE uid=?", (uid,)).fetchone()


def _device_public(row: sqlite3.Row, owner: str | None = None) -> dict:
    return {
        "uid": row["uid"],
        "name": row["name"] or row["uid"],
        "board": row["board"],
        "fw": row["fw"],
        "owner": owner,
        "bound": row["owner_id"] is not None,
        "created_at": row["created_at"],
        "last_seen": row["last_seen"],
        "last_sync_at": row["last_sync_at"],
        "data_version": row["data_version"],
    }


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


class BindBody(BaseModel):
    device_uid: str = Field(..., min_length=3, max_length=64)


class TrackPayload(BaseModel):
    local_id: int
    hash: str | None = None
    payload: dict | None = None
    deleted: bool = False


class SessionPayload(BaseModel):
    local_slot: int
    hash: str | None = None
    payload: dict | None = None
    csv: str | None = None
    csv_b64: str | None = None
    csv_gzip_b64: str | None = None
    deleted: bool = False


class LogPayload(BaseModel):
    local_index: int
    hash: str | None = None
    payload: dict | None = None
    deleted: bool = False


class DeviceRegisterBody(BaseModel):
    device_uid: str = Field(..., min_length=3, max_length=64)
    device_secret: str = Field(..., min_length=16, max_length=256)
    board: str = ""
    fw: str = ""
    name: str = ""


class SyncBody(BaseModel):
    device_uid: str = Field(..., min_length=3, max_length=64)
    device_secret: str = Field(..., min_length=16, max_length=256)
    since: int = 0
    tracks: list[TrackPayload] = []
    sessions: list[SessionPayload] = []
    logs: list[LogPayload] = []
    command_acks: list[int] = []


class PhoneSyncBody(BaseModel):
    since: int = 0
    replace: bool = True
    tracks: list[TrackPayload] = []
    sessions: list[SessionPayload] = []
    logs: list[LogPayload] = []


class TrackEditBody(BaseModel):
    name: str | None = None
    start_f: float | None = None
    start_width: float | None = None
    start_offset: float | None = None
    reverse: bool | None = None


# ---------------------------------------------------------------------------
# track processing
# ---------------------------------------------------------------------------

def _point_pairs(payload: dict) -> list[list[float]]:
    raw = payload.get("points") or []
    if not raw:
        return []
    if isinstance(raw[0], (list, tuple)):
        return [[float(p[0]), float(p[1])] for p in raw if len(p) >= 2]
    out = []
    for i in range(0, len(raw) - 1, 2):
        out.append([float(raw[i]), float(raw[i + 1])])
    return out


def _track_view(row: sqlite3.Row, include_payload: bool = True) -> dict:
    payload = json.loads(row["payload"])
    out = {
        "local_id": row["local_id"],
        "track_uuid": row["track_uuid"],
        "version": row["track_version"],
        "revision_id": row["revision_id"],
        "parent_revision_id": row["parent_revision_id"],
        "name": row["name"],
        "hash": row["content_hash"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted": bool(row["deleted"]),
    }
    if include_payload and not row["deleted"]:
        out["payload"] = payload
    return out


def _clean_track_payload(payload: dict) -> dict:
    payload = dict(payload or {})
    payload["points"] = _point_pairs(payload)
    payload["name"] = str(payload.get("name") or "")[:48]
    for k in ("start_f", "start_width", "start_offset", "width", "length_m"):
        try:
            payload[k] = float(payload.get(k, 0.0) or 0.0)
        except Exception:
            payload[k] = 0.0
    payload["reverse"] = bool(payload.get("reverse"))
    payload["geo"] = bool(payload.get("geo"))
    return payload


def _upsert_track(conn: sqlite3.Connection, uid: str, item: TrackPayload) -> tuple[bool, str | None]:
    if item.payload is None:
        existing = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND local_id=?", (uid, item.local_id)
        ).fetchone()
        if item.deleted:
            if existing and not existing["deleted"]:
                rev = _next_revision(conn)
                conn.execute(
                    "UPDATE tracks SET deleted=1,revision=?,updated_at=? WHERE id=?",
                    (rev, _now(), existing["id"]),
                )
                return True, str(rev)
            return False, None
        if existing is None:
            return False, "upload"
        if item.hash and item.hash != existing["content_hash"]:
            return False, "upload"
        return False, None

    payload = _clean_track_payload(item.payload)
    digest = _content_hash(payload)
    track_uuid = str(payload.get("uuid") or "").strip()
    version = int(payload.get("version") or 1)
    revision_id = str(payload.get("revision_id") or "").strip()
    parent = str(payload.get("parent_revision_id") or "").strip() or None
    if not track_uuid:
        track_uuid = None
    if not revision_id and track_uuid:
        revision_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                     f"racebrick:trackrev:{track_uuid}:{version}:{digest}"))
    existing = None
    if track_uuid:
        existing = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND track_uuid=?", (uid, track_uuid)
        ).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND local_id=?", (uid, item.local_id)
        ).fetchone()
    if item.deleted:
        if existing and not existing["deleted"]:
            rev = _next_revision(conn)
            conn.execute(
                "UPDATE tracks SET deleted=1,revision=?,updated_at=? WHERE id=?",
                (rev, _now(), existing["id"]),
            )
            return True, str(rev)
        return False, None
    if existing and existing["content_hash"] == digest and not existing["deleted"]:
        return False, None
    # A Track revision may move to a different device slot. If a stale legacy
    # row still owns that slot, retire it before updating the UUID row.
    if existing and int(existing["local_id"]) != int(item.local_id):
        conflict = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND local_id=? AND id<>?",
            (uid, item.local_id, existing["id"]),
        ).fetchone()
        if conflict is not None:
            old_uuid = str(conflict["track_uuid"] or "").strip()
            if not old_uuid or conflict["deleted"]:
                rev0 = _next_revision(conn)
                conn.execute(
                    "UPDATE tracks SET deleted=1,revision=?,updated_at=? WHERE id=?",
                    (rev0, _now(), conflict["id"]),
                )
    rev = _next_revision(conn)
    now = _now()
    values = (
        payload.get("name", ""), _canonical(payload), digest, rev, now,
        item.local_id, track_uuid, revision_id, parent, version,
        existing["created_at"] if existing else now,
    )
    if existing:
        conn.execute(
            """UPDATE tracks SET name=?,payload=?,content_hash=?,revision=?,updated_at=?,
               local_id=?,track_uuid=?,revision_id=?,parent_revision_id=?,track_version=?,
               created_at=?,deleted=0 WHERE id=?""",
            values + (existing["id"],),
        )
    else:
        conn.execute(
            """INSERT INTO tracks(device_uid,name,payload,content_hash,revision,updated_at,
               local_id,track_uuid,revision_id,parent_revision_id,track_version,created_at,deleted)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (uid, *values),
        )
    if track_uuid and revision_id:
        conn.execute(
            """INSERT OR IGNORE INTO track_revisions
               (device_uid,track_uuid,revision_id,version,parent_revision_id,payload,
                content_hash,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (uid, track_uuid, revision_id, version, parent, _canonical(payload), digest,
             "device", now),
        )
    return True, str(rev)


# ---------------------------------------------------------------------------
# session processing
# ---------------------------------------------------------------------------

def _track_payload(conn: sqlite3.Connection, uid: str, local_id: int | None,
                   track_uuid: str | None = None) -> dict | None:
    row = None
    if track_uuid:
        row = conn.execute(
            "SELECT payload FROM tracks WHERE device_uid=? AND track_uuid=? AND deleted=0",
            (uid, track_uuid),
        ).fetchone()
    if row is None and local_id is not None:
        row = conn.execute(
            "SELECT payload FROM tracks WHERE device_uid=? AND local_id=? AND deleted=0",
            (uid, local_id),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def _geo_norm(lat: float, lon: float, track: dict | None) -> tuple[float, float]:
    if track and track.get("geo") and float(track.get("nscale") or 0) > 1e-9:
        lat0 = float(track.get("glat0") or 0)
        lon0 = float(track.get("glon0") or 0)
        nscale = float(track.get("nscale"))
        mx = (lon - lon0) * 111320.0 * math.cos(math.radians((lat + lat0) / 2))
        my = (lat - lat0) * 111320.0
        return mx * nscale + float(track.get("nox") or 0), my * nscale + float(track.get("noy") or 0)
    return lon, lat


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    kx = 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * 111320.0)


def _process_session(conn: sqlite3.Connection, uid: str, item: SessionPayload) -> dict:
    payload = dict(item.payload or {})
    csv_text = item.csv or ""
    lap_times = [float(x) for x in (payload.get("lap_times") or [])]
    deleted_mask = int(payload.get("deleted_laps") or 0)
    track_id = payload.get("track")
    track = _track_payload(conn, uid, int(track_id) if track_id is not None else None,
                           str(payload.get("track_uuid") or "") or None)
    rows: list[dict] = []
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 7:
            continue
        try:
            timestamp = float(row[0])
            lap = int(float(row[2]))
            elapsed = float(row[3])
            lat = float(row[4])
            lon = float(row[5])
            speed = float(row[6])
        except Exception:
            continue
        if lap <= 0:
            continue
        rows.append({"t": timestamp, "lap": lap, "elapsed": elapsed,
                     "lat": lat, "lon": lon, "kmh": speed})
    # Existing raw sessions keep the whole outlap in lap 1. The server cuts it
    # using the recorded lap time; new firmware no longer uploads the outlap at all.
    if rows and lap_times:
        last1 = max((r["elapsed"] for r in rows if r["lap"] == 1), default=None)
        if last1 is not None:
            cut = last1 - lap_times[0]
            if cut > 0:
                rows = [r for r in rows if not (r["lap"] == 1 and r["elapsed"] < cut)]
    by_lap: dict[int, list[dict]] = {}
    for r in rows:
        if deleted_mask & (1 << (r["lap"] - 1)):
            continue
        by_lap.setdefault(r["lap"], []).append(r)
    laps = []
    for lap in sorted(by_lap):
        rs = by_lap[lap]
        if len(rs) < 2:
            continue
        dist = 0.0
        for a, b in zip(rs, rs[1:]):
            dist += _dist_m(a["lat"], a["lon"], b["lat"], b["lon"])
        speeds = [r["kmh"] for r in rs]
        times = [r["elapsed"] for r in rs]
        step = max(1, math.ceil(len(rs) / 96))
        pts = []
        for i in range(0, len(rs), step):
            r = rs[i]
            x, y = _geo_norm(r["lat"], r["lon"], track)
            pts.append([round(x, 5), round(y, 5), round(r["kmh"], 1), round(r["elapsed"], 3)])
        lap_time = lap_times[lap - 1] if lap - 1 < len(lap_times) else (rs[-1]["elapsed"] - rs[0]["elapsed"])
        laps.append({
            "lap": lap,
            "time": round(float(lap_time), 3),
            "samples": len(rs),
            "distance_m": round(dist, 1),
            "avg_kmh": round(sum(speeds) / len(speeds), 1),
            "max_kmh": round(max(speeds), 1),
            "points": pts,
        })
    positive = sorted(l["distance_m"] for l in laps if l["distance_m"] > 0)
    typical = positive[(len(positive) - 1) // 2] if positive else 0.0
    complete = [l for l in laps if typical <= 0 or 0.70 * typical <= l["distance_m"] <= 1.30 * typical]
    best = min(complete or laps, key=lambda l: l["time"]) if laps else None
    summary = {
        "laps": laps,
        "best_lap": best["lap"] if best else None,
        "best_time": best["time"] if best else None,
    }
    return summary


def _upsert_session(conn: sqlite3.Connection, uid: str, item: SessionPayload) -> tuple[bool, str | None]:
    if item.csv is None and item.csv_b64:
        try:
            item.csv = base64.b64decode(item.csv_b64).decode("utf-8")
        except Exception:
            item.csv = None
    if item.csv is None and item.csv_gzip_b64:
        try:
            item.csv = gzip.decompress(base64.b64decode(item.csv_gzip_b64)).decode("utf-8")
        except Exception:
            item.csv = None

    supplied = dict(item.payload or {})
    session_uuid = str(supplied.get("session_uuid") or "").strip() or None
    existing = None
    if session_uuid:
        existing = conn.execute(
            "SELECT * FROM sessions WHERE device_uid=? AND session_uuid=?",
            (uid, session_uuid),
        ).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT * FROM sessions WHERE device_uid=? AND local_slot=?",
            (uid, item.local_slot),
        ).fetchone()
    if item.deleted:
        if existing and not existing["deleted"]:
            rev = _next_revision(conn)
            conn.execute("UPDATE sessions SET deleted=1,revision=?,updated_at=? WHERE id=?",
                         (rev, _now(), existing["id"]))
            return True, str(rev)
        return False, None
    if item.payload is None and item.csv is None:
        if existing is None:
            return False, "upload"
        if item.hash and item.hash != existing["content_hash"]:
            return False, "upload"
        return False, None
    if existing and item.payload is None:
        payload = json.loads(existing["payload"])
    else:
        payload = supplied
    csv_text = item.csv
    if csv_text is None and existing:
        csv_text = existing["csv"]
    digest = _content_hash(payload, csv_text)
    if existing and existing["content_hash"] == digest and not existing["deleted"]:
        return False, None

    if not session_uuid and existing and existing["session_uuid"]:
        session_uuid = existing["session_uuid"]
    if not session_uuid:
        session_uuid = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"racebrick:session:{uid}:{item.local_slot}:{digest}",
        ))

    track_uuid = str(payload.get("track_uuid") or "").strip() or None
    track_revision_id = str(payload.get("track_revision_id") or "").strip() or None
    track = payload.get("track")
    if track_uuid is None and track is not None:
        row = conn.execute(
            "SELECT track_uuid FROM tracks WHERE device_uid=? AND local_id=? AND deleted=0",
            (uid, int(track)),
        ).fetchone()
        track_uuid = row["track_uuid"] if row else None
    if track_revision_id is None and track_uuid:
        row = conn.execute(
            "SELECT revision_id FROM tracks WHERE device_uid=? AND track_uuid=? AND deleted=0",
            (uid, track_uuid),
        ).fetchone()
        track_revision_id = row["revision_id"] if row else None

    item.payload = payload
    item.csv = csv_text
    summary = _process_session(conn, uid, item)
    best = summary.get("best_lap")
    best_time = summary.get("best_time")
    rev = _next_revision(conn)
    now = _now()
    start_epoch = int(payload.get("start") or 0)
    duration_ms = int(payload.get("duration_ms") or 0)
    end_epoch = int(payload.get("end") or (start_epoch + duration_ms // 1000))
    track_version = int(payload.get("track_version") or 0)
    sample_size = int(payload.get("sample_size") or 16)
    if sample_size not in (16, 20):
        sample_size = 16
    metadata_hash = _content_hash(payload.get("metadata") or {})
    laps_hash = _content_hash(summary["laps"])
    telemetry_hash = hashlib.sha256((csv_text or "").encode("utf-8")).hexdigest()
    vals = (
        _canonical(payload), csv_text, _canonical(summary), digest, rev, now,
        track, str(payload.get("track_name") or ""), start_epoch, duration_ms,
        len(summary["laps"]), best, best_time,
        session_uuid, track_uuid, track_revision_id, track_version, end_epoch,
        sample_size, sample_size, metadata_hash, laps_hash, telemetry_hash,
    )
    if existing:
        conn.execute(
            """UPDATE sessions SET payload=?,csv=?,summary=?,content_hash=?,revision=?,
               updated_at=?,track_local_id=?,track_name=?,start_epoch=?,
               duration_ms=?,lap_count=?,best_lap=?,best_time=?,
               session_uuid=?,track_uuid=?,track_revision_id=?,track_version=?,
               end_epoch=?,sample_size=?,raw_format=?,metadata_hash=?,laps_hash=?,
               telemetry_hash=?,deleted=0 WHERE id=?""",
            vals + (existing["id"],),
        )
    else:
        conn.execute(
            """INSERT INTO sessions(device_uid,payload,csv,summary,content_hash,revision,
               updated_at,track_local_id,track_name,start_epoch,duration_ms,lap_count,
               best_lap,best_time,session_uuid,track_uuid,track_revision_id,track_version,
               end_epoch,sample_size,raw_format,metadata_hash,laps_hash,telemetry_hash,
               local_slot,deleted)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (uid, *vals[:13], session_uuid, track_uuid, track_revision_id, track_version,
             end_epoch, sample_size, sample_size, metadata_hash, laps_hash, telemetry_hash,
             item.local_slot),
        )

    conn.execute("DELETE FROM lap_records WHERE device_uid=? AND session_uuid=?",
                 (uid, session_uuid))
    supplied_lap_ids = payload.get("lap_ids") if isinstance(payload.get("lap_ids"), list) else []
    for idx, lap in enumerate(summary["laps"]):
        lap_number = int(lap["lap"])
        lap_id = str(supplied_lap_ids[idx]) if idx < len(supplied_lap_ids) else str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"racebrick:lap:{session_uuid}:{lap_number}",
        ))
        ref = json.dumps({"samples": int(lap.get("samples") or 0)},
                         ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """INSERT OR REPLACE INTO lap_records
               (device_uid,session_uuid,lap_id,lap_number,lap_time_ms,sector_times_ms,
                start_time,end_time,best_lap,telemetry_reference,content_hash,deleted,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (uid, session_uuid, lap_id, lap_number,
             int(round(float(lap.get("time") or 0.0) * 1000.0)),
             "[]", None, None,
             1 if best is not None and lap_number == int(best) else 0,
             ref, _content_hash(lap), now),
        )
    return True, str(rev)


def _upsert_log(conn: sqlite3.Connection, uid: str, item: LogPayload) -> tuple[bool, str | None]:
    existing = conn.execute(
        "SELECT * FROM race_logs WHERE device_uid=? AND local_index=?", (uid, item.local_index)
    ).fetchone()
    if item.deleted:
        if existing and not existing["deleted"]:
            rev = _next_revision(conn)
            conn.execute("UPDATE race_logs SET deleted=1,revision=?,updated_at=? WHERE id=?",
                         (rev, _now(), existing["id"]))
            return True, str(rev)
        return False, None
    if item.payload is None:
        if existing is None:
            return False, "upload"
        if item.hash and item.hash != existing["content_hash"]:
            return False, "upload"
        return False, None
    payload = dict(item.payload or {})
    digest = _content_hash(payload)
    if existing and existing["content_hash"] == digest and not existing["deleted"]:
        return False, None
    rev = _next_revision(conn)
    best_lap = payload.get("best_index")
    best_time = payload.get("best")
    if best_lap is not None:
        try:
            best_lap = int(best_lap)
        except Exception:
            best_lap = None
    if best_time is not None:
        try:
            best_time = float(best_time)
        except Exception:
            best_time = None
    if existing:
        conn.execute(
            """UPDATE race_logs SET payload=?,content_hash=?,revision=?,deleted=0,
               updated_at=?,track_name=?,lap_count=?,best_lap=?,best_time=? WHERE id=?""",
            (_canonical(payload), digest, rev, _now(), str(payload.get("track_name") or ""),
             int(payload.get("laps") or 0), best_lap, best_time, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO race_logs(device_uid,local_index,payload,content_hash,revision,
               deleted,updated_at,track_name,lap_count,best_lap,best_time)
               VALUES(?,?,?,?,?,0,?,?,?,?,?)""",
            (uid, item.local_index, _canonical(payload), digest, rev, _now(),
             str(payload.get("track_name") or ""), int(payload.get("laps") or 0),
             best_lap, best_time),
        )
    return True, str(rev)


def _session_view(row: sqlite3.Row) -> dict:
    return {
        "local_slot": row["local_slot"],
        "session_uuid": row["session_uuid"],
        "hash": row["content_hash"],
        "revision": row["revision"],
        "deleted": bool(row["deleted"]),
        "updated_at": row["updated_at"],
        "track_local_id": row["track_local_id"],
        "track_uuid": row["track_uuid"],
        "track_revision_id": row["track_revision_id"],
        "track_version": row["track_version"],
        "track_name": row["track_name"],
        "start_epoch": row["start_epoch"],
        "end_epoch": row["end_epoch"],
        "duration_ms": row["duration_ms"],
        "sample_size": row["sample_size"],
        "laps": row["lap_count"],
        "best_lap": row["best_lap"],
        "best_time": row["best_time"],
        "summary": json.loads(row["summary"]) if row["summary"] else {"laps": []},
    }


def _log_view(row: sqlite3.Row, include_payload: bool = True) -> dict:
    out = {
        "local_index": row["local_index"],
        "hash": row["content_hash"],
        "revision": row["revision"],
        "deleted": bool(row["deleted"]),
        "updated_at": row["updated_at"],
        "track_name": row["track_name"],
        "laps": row["lap_count"],
        "best_lap": row["best_lap"],
        "best_time": row["best_time"],
    }
    if include_payload and not row["deleted"]:
        out["payload"] = json.loads(row["payload"])
    return out


# ---------------------------------------------------------------------------
# endpoints: account
# ---------------------------------------------------------------------------

@router.post("/api/platform/register")
def register(body: RegisterBody):
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise HTTPException(409, "username already exists")
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?, 'user',?)",
            (body.username, _hash_password(body.password), _now()),
        )
        token = _make_token(conn, cur.lastrowid)
        return {"token": token, "user": {"id": cur.lastrowid, "username": body.username, "role": "user"}}


@router.post("/api/platform/login")
def login(body: LoginBody):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
        if not row or not _check_password(body.password, row["password_hash"]):
            raise HTTPException(401, "invalid username or password")
        return {"token": _make_token(conn, row["id"]), "user": _user_dict(row)}


@router.get("/api/platform/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


@router.post("/api/platform/logout")
def logout(
    authorization: str | None = Header(default=None),
    user: dict = Depends(current_user),
):
    if authorization:
        token = authorization[7:].strip()
        with db() as conn:
            conn.execute("DELETE FROM auth_tokens WHERE token_hash=?", (_token_hash(token),))
    return {"ok": True}


@router.get("/api/platform/devices")
def devices(user: dict = Depends(current_user)):
    with db() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                """SELECT d.*, u.username AS owner_name FROM devices d
                   LEFT JOIN users u ON u.id=d.owner_id ORDER BY d.last_seen DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT d.*, u.username AS owner_name FROM devices d
                   LEFT JOIN users u ON u.id=d.owner_id
                   WHERE d.owner_id=? ORDER BY d.last_seen DESC""",
                (user["id"],),
            ).fetchall()
        out = []
        for r in rows:
            d = _device_public(r, r["owner_name"])
            d["tracks"] = conn.execute(
                "SELECT COUNT(*) n FROM tracks WHERE device_uid=? AND deleted=0", (r["uid"],)
            ).fetchone()["n"]
            d["sessions"] = conn.execute(
                "SELECT COUNT(*) n FROM sessions WHERE device_uid=? AND deleted=0", (r["uid"],)
            ).fetchone()["n"]
            out.append(d)
        return {"devices": out}


@router.post("/api/platform/bind")
def bind(body: BindBody, user: dict = Depends(current_user)):
    uid = body.device_uid.strip()
    if not DEVICE_RE.match(uid):
        raise HTTPException(400, "invalid device uid")
    with db() as conn:
        row = _get_device(conn, uid)
        if row is None:
            conn.execute(
                "INSERT INTO devices(uid,name,created_at,owner_id) VALUES(?,?,?,?)",
                (uid, "RaceBrick " + uid[-6:], _now(), user["id"]),
            )
            return {"ok": True, "device_uid": uid, "created": True}
        if row["owner_id"] not in (None, user["id"]):
            raise HTTPException(409, "device is already bound to another account")
        if row["owner_id"] is None:
            conn.execute("UPDATE devices SET owner_id=? WHERE uid=?", (user["id"], uid))
        return {"ok": True, "device_uid": uid, "created": False}


def _owned_device(conn: sqlite3.Connection, uid: str, user: dict) -> sqlite3.Row:
    row = _get_device(conn, uid)
    if row is None:
        raise HTTPException(404, "device not found")
    if user["role"] != "admin" and row["owner_id"] != user["id"]:
        raise HTTPException(403, "device not bound to this account")
    return row


@router.get("/api/platform/devices/{uid}")
def device_detail(uid: str, user: dict = Depends(current_user)):
    with db() as conn:
        dev = _owned_device(conn, uid, user)
        owner = conn.execute("SELECT username FROM users WHERE id=?", (dev["owner_id"],)).fetchone()
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND deleted=0 ORDER BY local_id", (uid,)
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE device_uid=? AND deleted=0 ORDER BY local_slot DESC", (uid,)
        ).fetchall()
        logs = conn.execute(
            "SELECT * FROM race_logs WHERE device_uid=? AND deleted=0 ORDER BY local_index", (uid,)
        ).fetchall()
        commands = conn.execute(
            "SELECT * FROM commands WHERE device_uid=? AND status='pending' ORDER BY id", (uid,)
        ).fetchall()
        return {
            "device": _device_public(dev, owner["username"] if owner else None),
            "tracks": [_track_view(r) for r in tracks],
            "sessions": [_session_view(r) for r in sessions],
            "logs": [_log_view(r) for r in logs],
            "pending_commands": [dict(r) for r in commands],
        }


@router.get("/api/platform/devices/{uid}/sync-history")
def sync_history(uid: str, user: dict = Depends(current_user)):
    with db() as conn:
        _owned_device(conn, uid, user)
        rows = conn.execute(
            """SELECT id,created_at,revision,upload_bytes,track_count,
                      session_count,log_count FROM sync_history
               WHERE device_uid=? ORDER BY id DESC LIMIT 5""",
            (uid,),
        ).fetchall()
        return {"history": [dict(r) for r in rows]}


@router.get("/api/platform/devices/{uid}/sync-history/{history_id}")
def sync_history_detail(uid: str, history_id: int, user: dict = Depends(current_user)):
    with db() as conn:
        _owned_device(conn, uid, user)
        row = conn.execute(
            "SELECT * FROM sync_history WHERE id=? AND device_uid=?",
            (history_id, uid),
        ).fetchone()
        if not row:
            raise HTTPException(404, "sync history not found")
        try:
            snapshot = json.loads(zlib.decompress(row["payload"]))
        except Exception:
            snapshot = {"error": "snapshot decode failed"}
        return {"id": row["id"], "created_at": row["created_at"],
                "revision": row["revision"], "upload_bytes": row["upload_bytes"],
                "snapshot": snapshot}


@router.patch("/api/platform/devices/{uid}/tracks/{local_id}")
def edit_track(uid: str, local_id: int, body: TrackEditBody, user: dict = Depends(current_user)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields supplied")
    with db() as conn:
        _owned_device(conn, uid, user)
        row = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND local_id=? AND deleted=0",
            (uid, local_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "track not found")
        payload = json.loads(row["payload"])
        if "name" in fields:
            fields["name"] = str(fields["name"])[:48]
            payload["name"] = fields["name"]
        if "start_f" in fields:
            fields["start_f"] = max(0.0, min(0.9999, float(fields["start_f"])))
            payload["start_f"] = fields["start_f"]
        if "start_width" in fields:
            fields["start_width"] = max(1.0, min(50.0, float(fields["start_width"])))
            payload["start_width"] = fields["start_width"]
        if "start_offset" in fields:
            fields["start_offset"] = max(-50.0, min(50.0, float(fields["start_offset"])))
            payload["start_offset"] = fields["start_offset"]
        if "reverse" in fields:
            fields["reverse"] = bool(fields["reverse"])
            payload["reverse"] = fields["reverse"]
        digest = _content_hash(payload)
        rev = _next_revision(conn)
        conn.execute(
            "UPDATE tracks SET name=?,payload=?,content_hash=?,revision=?,updated_at=? WHERE id=?",
            (payload.get("name", ""), _canonical(payload), digest, rev, _now(), row["id"]),
        )
        dv = _bump_data_version(conn, uid)
        return {"ok": True, "revision": rev, "data_version": dv, "track": _track_view(
            conn.execute("SELECT * FROM tracks WHERE id=?", (row["id"],)).fetchone()
        )}


@router.get("/api/platform/admin/users")
def admin_users(user: dict = Depends(admin_user)):
    with db() as conn:
        rows = conn.execute(
            """SELECT u.id,u.username,u.role,u.created_at,
                      COUNT(d.uid) AS devices
               FROM users u LEFT JOIN devices d ON d.owner_id=u.id
               GROUP BY u.id ORDER BY u.id"""
        ).fetchall()
        return {"users": [dict(r) for r in rows]}


@router.get("/api/v2/devices/{uid}/manifest")
def cloud_manifest(uid: str, user: dict = Depends(current_user)):
    with db() as conn:
        _owned_device(conn, uid, user)
        return _cloud_manifest(conn, uid)


# ---------------------------------------------------------------------------
# endpoints: phone relay sync
# ---------------------------------------------------------------------------

def _bump_data_version(conn: sqlite3.Connection, uid: str) -> int:
    conn.execute(
        "UPDATE devices SET data_version=data_version+1 WHERE uid=?", (uid,)
    )
    row = conn.execute("SELECT data_version FROM devices WHERE uid=?", (uid,)).fetchone()
    return int(row["data_version"]) if row else 0


def _phone_device(conn: sqlite3.Connection, uid: str, user: dict) -> sqlite3.Row:
    uid = uid.strip()
    if not DEVICE_RE.match(uid):
        raise HTTPException(400, "invalid device uid")
    dev = _get_device(conn, uid)
    if dev is None:
        conn.execute(
            "INSERT INTO devices(uid,name,created_at,owner_id) VALUES(?,?,?,?)",
            (uid, "RaceBrick " + uid[-6:], _now(), user["id"]),
        )
        dev = _get_device(conn, uid)
    elif dev["owner_id"] is None:
        conn.execute("UPDATE devices SET owner_id=? WHERE uid=?", (user["id"], uid))
        dev = _get_device(conn, uid)
    elif user["role"] != "admin" and dev["owner_id"] != user["id"]:
        raise HTTPException(403, "device is already bound to another account")
    return dev


def _record_snapshot(conn: sqlite3.Connection, uid: str, revision: int,
                     payload: dict, upload_bytes: int,
                     track_count: int, session_count: int, log_count: int) -> int:
    clean = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    cur = conn.execute(
        """INSERT INTO sync_history(device_uid,created_at,revision,upload_bytes,
           track_count,session_count,log_count,payload)
           VALUES(?,?,?,?,?,?,?,?)""",
        (uid, _now(), revision, upload_bytes, track_count, session_count,
         log_count, zlib.compress(clean, 6)),
    )
    keep = conn.execute(
        "SELECT id FROM sync_history WHERE device_uid=? ORDER BY id DESC LIMIT 5",
        (uid,),
    ).fetchall()
    ids = ",".join(str(r["id"]) for r in keep)
    if ids:
        conn.execute(
            f"DELETE FROM sync_history WHERE device_uid=? AND id NOT IN ({ids})",
            (uid,),
        )
    return int(cur.lastrowid)


def _cleanup_legacy_tracks(conn: sqlite3.Connection, uid: str) -> int:
    """Remove legacy track rows that have no UUID when a UUID track with the
    same name exists. This clears duplicate rows created by pre-UUID uploads
    without touching a genuinely unique legacy track."""
    rows = conn.execute(
        """SELECT t.id,t.name FROM tracks t
           WHERE t.device_uid=? AND t.deleted=0 AND (t.track_uuid IS NULL OR t.track_uuid='')
             AND EXISTS (SELECT 1 FROM tracks u
                         WHERE u.device_uid=t.device_uid AND u.deleted=0
                           AND u.track_uuid IS NOT NULL AND u.track_uuid<>''
                           AND u.name=t.name)""",
        (uid,),
    ).fetchall()
    changed = 0
    for r in rows:
        rev = _next_revision(conn)
        conn.execute(
            "UPDATE tracks SET deleted=1,revision=?,updated_at=? WHERE id=?",
            (rev, _now(), r["id"]),
        )
        changed += 1
    return changed


def _mark_missing_deleted(conn: sqlite3.Connection, table: str, key: str,
                          uid: str, present: set[int]) -> int:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE device_uid=? AND deleted=0", (uid,)
    ).fetchall()
    changed = 0
    for row in rows:
        if int(row[key]) in present:
            continue
        rev = _next_revision(conn)
        conn.execute(
            f"UPDATE {table} SET deleted=1,revision=?,updated_at=? WHERE id=?",
            (rev, _now(), row["id"]),
        )
        changed += 1
    return changed


def _cloud_manifest(conn: sqlite3.Connection, uid: str) -> dict:
    dev = _get_device(conn, uid)
    tracks = conn.execute(
        "SELECT * FROM tracks WHERE device_uid=? AND deleted=0 ORDER BY name, track_uuid",
        (uid,),
    ).fetchall()
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE device_uid=? AND deleted=0 ORDER BY start_epoch DESC",
        (uid,),
    ).fetchall()
    return {
        "protocol": 2,
        "device_id": uid,
        "generated_at": _now(),
        "revision": _revision(conn),
        "data_version": int(dev["data_version"]) if dev else 0,
        "tracks": [
            {
                "track_id": r["track_uuid"],
                "local_slot": r["local_id"],
                "name": r["name"],
                "version": r["track_version"],
                "revision_id": r["revision_id"],
                "parent_revision_id": r["parent_revision_id"],
                "content_hash": r["content_hash"],
                "updated_at": r["updated_at"],
            }
            for r in tracks
        ],
        "sessions": [
            {
                "session_id": r["session_uuid"],
                "local_slot": r["local_slot"],
                "track_id": r["track_uuid"],
                "track_revision_id": r["track_revision_id"],
                "track_version": r["track_version"],
                "start": r["start_epoch"],
                "end": r["end_epoch"],
                "laps": r["lap_count"],
                "best_lap": r["best_lap"],
                "best_time": r["best_time"],
                "sample_size": r["sample_size"],
                "content_hash": r["content_hash"],
                "telemetry_available": bool(r["csv"]),
            }
            for r in sessions
        ],
    }


@router.get("/api/phone/v1/devices/{uid}/state")
def phone_state(uid: str, user: dict = Depends(current_user)):
    with db() as conn:
        dev = _phone_device(conn, uid, user)
        return {
            "device": _device_public(dev),
            "revision": _revision(conn),
            "data_version": dev["data_version"],
            "tracks": conn.execute(
                "SELECT COUNT(*) AS n FROM tracks WHERE device_uid=? AND deleted=0", (uid,)
            ).fetchone()["n"],
            "sessions": conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE device_uid=? AND deleted=0", (uid,)
            ).fetchone()["n"],
            "logs": conn.execute(
                "SELECT COUNT(*) AS n FROM race_logs WHERE device_uid=? AND deleted=0", (uid,)
            ).fetchone()["n"],
            "history": [dict(r) for r in conn.execute(
                """SELECT id,created_at,revision,upload_bytes,track_count,
                          session_count,log_count FROM sync_history
                   WHERE device_uid=? ORDER BY id DESC LIMIT 5""", (uid,)
            ).fetchall()],
        }


@router.get("/api/phone/v1/devices/{uid}/changes")
def phone_changes(uid: str, since: int = 0, user: dict = Depends(current_user)):
    with db() as conn:
        _phone_device(conn, uid, user)
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND revision>? ORDER BY local_id",
            (uid, since),
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE device_uid=? AND revision>? ORDER BY local_slot",
            (uid, since),
        ).fetchall()
        logs = conn.execute(
            "SELECT * FROM race_logs WHERE device_uid=? AND revision>? ORDER BY local_index",
            (uid, since),
        ).fetchall()
        return {
            "revision": _revision(conn),
            "tracks": [_track_view(r) for r in tracks],
            "sessions": [_session_view(r) for r in sessions],
            "logs": [_log_view(r) for r in logs],
        }


@router.post("/api/phone/v1/devices/{uid}/sync")
async def phone_sync(uid: str, request: Request, user: dict = Depends(current_user)):
    raw = await request.body()
    try:
        body = PhoneSyncBody.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(422, f"invalid phone sync body: {exc}")
    with db() as conn:
        _phone_device(conn, uid, user)
        track_ids = {int(t.local_id) for t in body.tracks if not t.deleted}
        session_ids = {int(s.local_slot) for s in body.sessions if not s.deleted}
        log_ids = {int(l.local_index) for l in body.logs if not l.deleted}
        changed = False
        for t in body.tracks:
            c, _ = _upsert_track(conn, uid, t)
            changed |= c
        for s in body.sessions:
            c, _ = _upsert_session(conn, uid, s)
            changed |= c
        for l in body.logs:
            c, _ = _upsert_log(conn, uid, l)
            changed |= c
        if body.replace:
            changed |= _mark_missing_deleted(conn, "tracks", "local_id", uid, track_ids) > 0
            changed |= _mark_missing_deleted(conn, "sessions", "local_slot", uid, session_ids) > 0
            changed |= _mark_missing_deleted(conn, "race_logs", "local_index", uid, log_ids) > 0
        changed |= _cleanup_legacy_tracks(conn, uid) > 0
        dv = _bump_data_version(conn, uid) if changed else int(conn.execute(
            "SELECT data_version FROM devices WHERE uid=?", (uid,)).fetchone()["data_version"])
        rev = _revision(conn)
        now = _now()
        conn.execute(
            "UPDATE devices SET last_seen=?,last_sync_at=?,last_sync_revision=? WHERE uid=?",
            (now, now, rev, uid),
        )
        hid = _record_snapshot(
            conn, uid, rev, body.model_dump(), len(raw),
            len(track_ids), len(session_ids), len(log_ids),
        )
        return {
            "ok": True,
            "revision": rev,
            "data_version": dv,
            "synced_at": now,
            "history_id": hid,
            "tracks": len(track_ids),
            "sessions": len(session_ids),
            "logs": len(log_ids),
        }


@router.get("/api/phone/v1/devices/{uid}/snapshot")
def phone_snapshot(uid: str, user: dict = Depends(current_user)):
    with db() as conn:
        dev = _phone_device(conn, uid, user)
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND deleted=0 ORDER BY local_id", (uid,)
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE device_uid=? AND deleted=0 ORDER BY local_slot", (uid,)
        ).fetchall()
        logs = conn.execute(
            "SELECT * FROM race_logs WHERE device_uid=? AND deleted=0 ORDER BY local_index", (uid,)
        ).fetchall()
        session_views = []
        for r in sessions:
            v = _session_view(r)
            try:
                v["payload"] = json.loads(r["payload"])
            except Exception:
                v["payload"] = {}
            if r["csv"]:
                v["csv"] = r["csv"]
            session_views.append(v)
        return {
            "device": _device_public(dev),
            "revision": _revision(conn),
            "data_version": dev["data_version"],
            "tracks": [_track_view(r) for r in tracks],
            "sessions": session_views,
            "logs": [_log_view(r) for r in logs],
        }


@router.get("/api/phone/v1/devices/{uid}/history")
def phone_history(uid: str, user: dict = Depends(current_user)):
    with db() as conn:
        _phone_device(conn, uid, user)
        rows = conn.execute(
            """SELECT id,created_at,revision,upload_bytes,track_count,session_count,
                      log_count FROM sync_history WHERE device_uid=?
               ORDER BY id DESC LIMIT 5""",
            (uid,),
        ).fetchall()
        return {"history": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# endpoints: device provisioning + sync
# ---------------------------------------------------------------------------

@router.post("/api/device/v1/register")
def device_register(body: DeviceRegisterBody):
    if not ALLOW_DIRECT_DEVICE:
        raise HTTPException(410, "direct device sync retired; use phone relay sync")
    uid = body.device_uid.strip()
    if not DEVICE_RE.match(uid):
        raise HTTPException(400, "invalid device uid")
    with db() as conn:
        row = _get_device(conn, uid)
        secret_hash = _hash_secret(body.device_secret)
        if row is None:
            conn.execute(
                """INSERT INTO devices(uid,name,board,fw,secret_hash,created_at,last_seen)
                   VALUES(?,?,?,?,?,?,?)""",
                (uid, body.name or ("RaceBrick " + uid[-6:]), body.board, body.fw,
                 secret_hash, _now(), _now()),
            )
            return {"ok": True, "created": True, "device_uid": uid}
        if row["secret_hash"] and row["secret_hash"] != secret_hash:
            raise HTTPException(401, "device secret mismatch")
        conn.execute(
            "UPDATE devices SET secret_hash=?,board=?,fw=?,name=COALESCE(NULLIF(?,''),name),last_seen=? WHERE uid=?",
            (secret_hash, body.board or row["board"], body.fw or row["fw"],
             body.name, _now(), uid),
        )
        return {"ok": True, "created": False, "device_uid": uid,
                "bound": row["owner_id"] is not None}


@router.post("/api/device/v1/sync")
async def device_sync(request: Request):
    if not ALLOW_DIRECT_DEVICE:
        raise HTTPException(410, "direct device sync retired; use phone relay sync")
    raw = await request.body()
    try:
        body = SyncBody.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(422, f"invalid sync body: {exc}")
    uid = body.device_uid.strip()
    if not DEVICE_RE.match(uid):
        raise HTTPException(400, "invalid device uid")
    nodata = request.query_params.get("nodata") == "1"
    with db() as conn:
        dev = _get_device(conn, uid)
        if dev is None:
            raise HTTPException(404, "device not registered")
        secret_hash = _hash_secret(body.device_secret)
        if not dev["secret_hash"] or dev["secret_hash"] != secret_hash:
            raise HTTPException(401, "device secret mismatch")
        need_tracks: list[int] = []
        need_sessions: list[int] = []
        need_logs: list[int] = []
        acked = set(body.command_acks)
        protected = set()
        for cmd in conn.execute(
            "SELECT * FROM commands WHERE device_uid=? AND status='pending' AND kind='track_update'",
            (uid,),
        ).fetchall():
            if cmd["id"] in acked:
                continue
            try:
                protected.add(int(json.loads(cmd["payload"]).get("track_local_id")))
            except Exception:
                pass
        changed = False
        for t in body.tracks:
            if t.local_id in protected:
                continue
            c, what = _upsert_track(conn, uid, t)
            changed |= c
            if what == "upload":
                need_tracks.append(t.local_id)
        for s in body.sessions:
            c, what = _upsert_session(conn, uid, s)
            changed |= c
            if what == "upload":
                need_sessions.append(s.local_slot)
        for l in body.logs:
            c, what = _upsert_log(conn, uid, l)
            changed |= c
            if what == "upload":
                need_logs.append(l.local_index)
        for cid in body.command_acks:
            conn.execute(
                "UPDATE commands SET status='acked',ack_at=? WHERE id=? AND device_uid=?",
                (_now(), cid, uid),
            )
        rev = _revision(conn)
        now = _now()
        conn.execute(
            """UPDATE devices SET last_seen=?,last_sync_at=?,last_sync_revision=?,
               board=COALESCE(NULLIF(board,''),board),fw=COALESCE(NULLIF(fw,''),fw) WHERE uid=?""",
            (now, now, rev, uid),
        )
        # Fetch after writes so the response always contains committed data.
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE device_uid=? AND revision>? ORDER BY local_id",
            (uid, body.since),
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE device_uid=? AND revision>? ORDER BY local_slot",
            (uid, body.since),
        ).fetchall()
        logs = conn.execute(
            "SELECT * FROM race_logs WHERE device_uid=? AND revision>? ORDER BY local_index",
            (uid, body.since),
        ).fetchall()
        commands = conn.execute(
            "SELECT * FROM commands WHERE device_uid=? AND status='pending' ORDER BY id",
            (uid,),
        ).fetchall()
        snapshot = body.model_dump()
        snapshot["device_secret"] = "redacted"
        clean = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        conn.execute(
            """INSERT INTO sync_history(device_uid,created_at,revision,upload_bytes,
               track_count,session_count,log_count,payload)
               VALUES(?,?,?,?,?,?,?,?)""",
            (uid, now, rev, len(raw), len(body.tracks), len(body.sessions),
             len(body.logs), zlib.compress(clean, 6)),
        )
        keep = conn.execute(
            "SELECT id FROM sync_history WHERE device_uid=? ORDER BY id DESC LIMIT 5",
            (uid,),
        ).fetchall()
        ids = ",".join(str(r["id"]) for r in keep)
        if ids:
            conn.execute(
                f"DELETE FROM sync_history WHERE device_uid=? AND id NOT IN ({ids})",
                (uid,),
            )
        return {
            "ok": True,
            "server_time": now,
            "synced_at": now,
            "revision": rev,
            "changed": changed,
            "tracks": [] if nodata else [_track_view(r) for r in tracks],
            "sessions": [] if nodata else [_session_view(r) for r in sessions],
            "logs": [] if nodata else [_log_view(r) for r in logs],
            "commands": [] if nodata else [dict(r) for r in commands],
            "need_tracks": need_tracks,
            "need_sessions": need_sessions,
            "need_logs": need_logs,
        }


# ---------------------------------------------------------------------------
# firmware-friendly device command queue
# ---------------------------------------------------------------------------

def _verify_device(conn: sqlite3.Connection, uid: str, secret: str) -> sqlite3.Row:
    dev = _get_device(conn, uid.strip())
    if not dev or not dev["secret_hash"] or dev["secret_hash"] != _hash_secret(secret):
        raise HTTPException(401, "device auth failed")
    return dev


@router.get("/api/device/v1/commands")
def device_commands(uid: str, secret: str | None = None,
                    x_device_secret: str | None = Header(None)):
    if not ALLOW_DIRECT_DEVICE:
        raise HTTPException(410, "direct device sync retired; use phone relay sync")
    secret = secret or x_device_secret or ""
    with db() as conn:
        _verify_device(conn, uid, secret)
        rows = conn.execute(
            "SELECT * FROM commands WHERE device_uid=? AND status='pending' ORDER BY id",
            (uid,),
        ).fetchall()
        lines = []
        for row in rows:
            if row["kind"] != "track_update":
                continue
            try:
                p = json.loads(row["payload"])
                f = p.get("fields", {})
                local_id = int(p.get("track_local_id", -1))
                tr = conn.execute(
                    "SELECT payload FROM tracks WHERE device_uid=? AND local_id=? AND deleted=0",
                    (uid, local_id),
                ).fetchone()
                base = json.loads(tr["payload"]) if tr else {}
                name = str(f.get("name", base.get("name", "")))[:48]
                name_b64 = base64.b64encode(name.encode("utf-8")).decode("ascii")
                lines.append("%d|%d|%.5f|%.2f|%.2f|%d|%s" % (
                    int(row["id"]), local_id,
                    float(f.get("start_f", base.get("start_f", 0))),
                    float(f.get("start_width", base.get("start_width", base.get("width", 0)))),
                    float(f.get("start_offset", base.get("start_offset", 0))),
                    1 if f.get("reverse", base.get("reverse", False)) else 0,
                    name_b64,
                ))
            except Exception:
                continue
        return Response("\n".join(lines) + ("\n" if lines else ""),
                        media_type="text/plain")


@router.post("/api/device/v1/ack")
def device_ack(uid: str, id: int, secret: str | None = None,
               x_device_secret: str | None = Header(None)):
    if not ALLOW_DIRECT_DEVICE:
        raise HTTPException(410, "direct device sync retired; use phone relay sync")
    secret = secret or x_device_secret or ""
    with db() as conn:
        _verify_device(conn, uid, secret)
        conn.execute(
            "UPDATE commands SET status='acked',ack_at=? WHERE id=? AND device_uid=?",
            (_now(), id, uid),
        )
        return {"ok": True}


# ---------------------------------------------------------------------------
# static account page
# ---------------------------------------------------------------------------

@router.get("/", include_in_schema=False)
def account_page():
    return FileResponse(ROOT / "static" / "account.html")


_init_db()
