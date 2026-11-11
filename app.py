"""MemoryLink photoframe server.

A small FastAPI service that:
  * stores images,
  * exposes a browser UI to upload / delete / edit them,
  * tells the Android frame app what to show right now.

Run:

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000

The Android app is configured with the public HTTPS URL only, so put a
reverse proxy (Caddy / nginx) in front of this service in production.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.getenv("MEMORYLINK_DATA_DIR", str(BASE_DIR / "data"))
).expanduser().resolve()
THUMBS_DIR = DATA_DIR / "thumbs"
SCALED_DIR = DATA_DIR / "scaled"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "frame.db"
UPDATE_PATH = DATA_DIR / "update.apk"
GITHUB_TOKEN_PATH = DATA_DIR / "github_token"

DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
SCALED_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

FRAME_ID = "default"
DEFAULT_INTERVAL = 30
FRAME_W, FRAME_H = 1080, 1920
MAX_APK_BYTES = 200 * 1024 * 1024
MAX_GITHUB_DOWNLOAD_BYTES = MAX_APK_BYTES + 20 * 1024 * 1024
GITHUB_REPO = os.getenv("MEMORYLINK_GITHUB_REPO", "rsnel/memorylink-app")
# The frame polls every ~6 s; allow a few missed polls before calling it offline.
ONLINE_TIMEOUT_SECONDS = 20

# Bump this together with the VERSION file when the server code changes. If the
# file is newer than this constant, the running process is out of date.
SERVER_VERSION = 10
VERSION_PATH = BASE_DIR / "VERSION"

# Must match the Transition enum names in the Android app.
TRANSITIONS = [
    "DISSOLVE",
    "SLIDE_LEFT",
    "SLIDE_RIGHT",
    "SLIDE_UP",
    "SLIDE_DOWN",
    "ZOOM_IN",
    "ZOOM_OUT",
]

app = FastAPI(title="MemoryLink Photoframe Server")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_name TEXT,
                width INTEGER,
                height INTEGER,
                position INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS image_tags (
                image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (image_id, tag_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS bluetooth_devices (
                address TEXT PRIMARY KEY,
                name TEXT,
                rssi INTEGER,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                comment TEXT
            )
            """
        )
        # Older databases predate the sighting counter.
        bt_cols = {r["name"] for r in db.execute("PRAGMA table_info(bluetooth_devices)").fetchall()}
        if "seen_count" not in bt_cols:
            db.execute(
                "ALTER TABLE bluetooth_devices ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 1"
            )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS frame_state (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'slideshow',
                current_image_id INTEGER,
                interval_seconds INTEGER NOT NULL DEFAULT 30,
                version INTEGER NOT NULL DEFAULT 0,
                tag_filter TEXT NOT NULL DEFAULT '[]',
                exclude_tags TEXT NOT NULL DEFAULT '[]',
                match_all INTEGER NOT NULL DEFAULT 1,
                random INTEGER NOT NULL DEFAULT 0,
                show_clock INTEGER NOT NULL DEFAULT 0,
                blur_background INTEGER NOT NULL DEFAULT 0,
                transitions TEXT NOT NULL DEFAULT '[]',
                shown_image_id INTEGER,
                shown_at TEXT,
                last_seen_at TEXT,
                restart_id INTEGER NOT NULL DEFAULT 0,
                frame_version_code INTEGER,
                frame_version_name TEXT,
                frame_version_at TEXT,
                update_id INTEGER NOT NULL DEFAULT 0,
                update_name TEXT,
                update_sha256 TEXT,
                update_size INTEGER,
                update_uploaded_at TEXT,
                update_status TEXT,
                update_error TEXT,
                update_result_at TEXT
            )
            """
        )
        # Migrate existing databases that predate the newer columns.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(frame_state)").fetchall()}
        if "tag_filter" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN tag_filter TEXT NOT NULL DEFAULT '[]'")
        if "exclude_tags" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN exclude_tags TEXT NOT NULL DEFAULT '[]'")
        if "match_all" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN match_all INTEGER NOT NULL DEFAULT 1")
        if "random" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN random INTEGER NOT NULL DEFAULT 0")
        if "show_clock" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN show_clock INTEGER NOT NULL DEFAULT 0")
        if "blur_background" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN blur_background INTEGER NOT NULL DEFAULT 0")
        if "transitions" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN transitions TEXT NOT NULL DEFAULT '[]'")
        if "shown_image_id" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN shown_image_id INTEGER")
        if "shown_at" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN shown_at TEXT")
        if "last_seen_at" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN last_seen_at TEXT")
        if "restart_id" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN restart_id INTEGER NOT NULL DEFAULT 0")
        if "frame_version_code" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN frame_version_code INTEGER")
        if "frame_version_name" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN frame_version_name TEXT")
        if "frame_version_at" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN frame_version_at TEXT")
        if "update_id" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_id INTEGER NOT NULL DEFAULT 0")
        if "update_name" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_name TEXT")
        if "update_sha256" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_sha256 TEXT")
        if "update_size" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_size INTEGER")
        if "update_uploaded_at" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_uploaded_at TEXT")
        if "update_status" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_status TEXT")
        if "update_error" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_error TEXT")
        if "update_result_at" not in cols:
            db.execute("ALTER TABLE frame_state ADD COLUMN update_result_at TEXT")

        # Give existing libraries a manual order: the order they were shown in
        # before reordering existed (newest first).
        image_cols = {r["name"] for r in db.execute("PRAGMA table_info(images)").fetchall()}
        if "position" not in image_cols:
            db.execute("ALTER TABLE images ADD COLUMN position INTEGER")
            db.execute(
                "UPDATE images SET position = "
                "(SELECT COUNT(*) FROM images newer WHERE newer.id > images.id)"
            )
        # Any rows that somehow lack a position go to the end of the order.
        null_rows = db.execute("SELECT id FROM images WHERE position IS NULL ORDER BY id").fetchall()
        if null_rows:
            next_pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM images").fetchone()[0]
            for offset, row in enumerate(null_rows):
                db.execute(
                    "UPDATE images SET position = ? WHERE id = ?",
                    (next_pos + offset, row["id"]),
                )
        db.execute(
            """
            INSERT OR IGNORE INTO frame_state
                (id, mode, current_image_id, interval_seconds, version, tag_filter, match_all)
            VALUES (?, 'slideshow', NULL, ?, 0, '[]', 1)
            """,
            (FRAME_ID, DEFAULT_INTERVAL),
        )


def bump_version(db) -> None:
    db.execute("UPDATE frame_state SET version = version + 1 WHERE id = ?", (FRAME_ID,))


def normalize_tag(name: str) -> str:
    return " ".join(name.strip().lower().split())


def image_tags(db, image_id: int) -> list[str]:
    rows = db.execute(
        "SELECT t.name FROM tags t JOIN image_tags it ON it.tag_id = t.id "
        "WHERE it.image_id = ? ORDER BY t.name",
        (image_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def set_image_tags(db, image_id: int, tags: list[str]) -> list[str]:
    """Replace the image's tags and drop any tags that become unused."""
    db.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
    cleaned: list[str] = []
    for raw in tags:
        name = normalize_tag(raw)
        if not name or name in cleaned:
            continue
        row = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row is None:
            cur = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            tag_id = cur.lastrowid
        else:
            tag_id = row["id"]
        db.execute(
            "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)",
            (image_id, tag_id),
        )
        cleaned.append(name)
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM image_tags)")
    return cleaned


def all_tags(db) -> list[dict]:
    rows = db.execute(
        "SELECT t.name, COUNT(it.image_id) AS count FROM tags t "
        "LEFT JOIN image_tags it ON it.tag_id = t.id "
        "GROUP BY t.id ORDER BY t.name"
    ).fetchall()
    return [{"name": r["name"], "count": r["count"]} for r in rows]


def image_dict(db, row, tags: list[str] | None = None) -> dict:
    if tags is None:
        tags = image_tags(db, row["id"])
    return {
        "id": row["id"],
        "url": f"/images/{row['filename']}",
        "thumb": f"/thumbs/{row['filename']}",
        "name": row["original_name"],
        "width": row["width"],
        "height": row["height"],
        "created_at": row["created_at"],
        "tags": tags,
    }


def frame_online(state) -> bool:
    """True if the frame checked in recently."""
    seen = state["last_seen_at"]
    if not seen:
        return False
    try:
        seen_at = datetime.fromisoformat(seen)
    except ValueError:
        return False
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - seen_at).total_seconds()
    return age <= ONLINE_TIMEOUT_SECONDS


def server_files_version() -> int:
    """Version marker on disk, so a running old process can spot a deployment."""
    try:
        return int(VERSION_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return SERVER_VERSION


def queued_build_number(update_name: str | None) -> int | None:
    if not update_name:
        return None
    match = re.search(r"(\d+)(?:-debug)?\.apk$", update_name)
    return int(match.group(1)) if match else None


def version_info(state) -> dict:
    files_version = server_files_version()
    queued_build = queued_build_number(state["update_name"])
    frame_code = state["frame_version_code"]
    update_status = state["update_status"] or "pending"
    frame_behind = bool(
        frame_code
        and queued_build
        and queued_build > frame_code
        and update_status not in ("installed", "canceled", "installing")
    )
    return {
        "server_version": SERVER_VERSION,
        "server_files_version": files_version,
        "server_behind": files_version > SERVER_VERSION,
        "restart_supported": True,
        "frame_version_code": frame_code,
        "frame_version_name": state["frame_version_name"],
        "queued_update_build": queued_build,
        "frame_behind": frame_behind,
    }


def update_dict(state) -> dict | None:
    """Describe the pushed APK, if there is one the frame can fetch."""
    if not state["update_id"] or not UPDATE_PATH.is_file():
        return None
    return {
        "id": state["update_id"],
        "url": "/update.apk",
        "name": state["update_name"],
        "sha256": state["update_sha256"],
        "size": state["update_size"],
        "uploaded_at": state["update_uploaded_at"],
        "status": state["update_status"] or "pending",
        "error": state["update_error"],
        "result_at": state["update_result_at"],
    }


def list_images(
    db,
    tag_filter: list[str] | None = None,
    match_all: bool = True,
    exclude_tags: list[str] | None = None,
) -> list[dict]:
    """All images, optionally restricted by include and exclude tag filters.

    With match_all=True an image must carry every included tag (AND); with
    match_all=False it must carry at least one (OR). An image carrying any
    excluded tag is always skipped.
    """
    rows = db.execute(
        "SELECT * FROM images ORDER BY position IS NULL, position, id DESC"
    ).fetchall()
    result = []
    for row in rows:
        tags = image_tags(db, row["id"])
        if tag_filter:
            if match_all:
                if not all(t in tags for t in tag_filter):
                    continue
            elif not any(t in tags for t in tag_filter):
                continue
        if exclude_tags and any(t in tags for t in exclude_tags):
            continue
        result.append(image_dict(db, row, tags))
    return result


def insert_image(filename: str, original_name: str, width: int, height: int) -> dict:
    with db_conn() as db:
        position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM images").fetchone()[0]
        cur = db.execute(
            "INSERT INTO images (filename, original_name, width, height, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (filename, original_name, width, height, position, now()),
        )
        bump_version(db)
        row = db.execute("SELECT * FROM images WHERE id = ?", (cur.lastrowid,)).fetchone()
        return image_dict(db, row)


def safe_filename(filename: str) -> Path:
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(status_code=404, detail="not found")
    return DATA_DIR / filename


_scaled_locks: dict[str, threading.Lock] = {}
_scaled_locks_guard = threading.Lock()


def _scaled_lock(filename: str) -> threading.Lock:
    with _scaled_locks_guard:
        lock = _scaled_locks.get(filename)
        if lock is None:
            lock = threading.Lock()
            _scaled_locks[filename] = lock
        return lock


def scaled_image_path(filename: str) -> Path:
    """Return a copy of the image capped to the frame size, cached on disk.

    The original is left untouched. Stored filenames never change (they are
    UUIDs), so a cache hit is always valid. Images that already fit are served
    from the originals and nothing is cached.
    """
    original = safe_filename(filename)
    if not original.is_file():
        return original

    cached = SCALED_DIR / filename
    if cached.is_file():
        return cached

    # Serialise generation per file so concurrent requests do not race.
    with _scaled_lock(filename):
        if cached.is_file():
            return cached
        tmp = None
        try:
            img = Image.open(original)
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.width <= FRAME_W and img.height <= FRAME_H:
                return original
            has_alpha = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            )
            img.thumbnail((FRAME_W, FRAME_H), Image.LANCZOS)
            tmp = cached.with_name(cached.name + ".tmp")
            if has_alpha:
                img.convert("RGBA").save(tmp, "PNG", optimize=True)
            else:
                img.convert("RGB").save(tmp, "JPEG", quality=92, optimize=True)
            tmp.replace(cached)
        except Exception:  # noqa: BLE001 - fall back to the original
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            return original

    return cached


def normalize_and_save(data: bytes) -> tuple[str, int, int]:
    """Return (stored filename, width, height), writing image + thumbnail."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Could not read image file") from exc

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    width, height = img.size
    filename = uuid.uuid4().hex + (".png" if has_alpha else ".jpg")

    if has_alpha:
        img = img.convert("RGBA")
        img.save(DATA_DIR / filename, "PNG", optimize=True)
    else:
        img = img.convert("RGB")
        img.save(DATA_DIR / filename, "JPEG", quality=92, optimize=True)

    thumb = img.copy()
    thumb.thumbnail((400, 400))
    thumb.convert("RGB").save(THUMBS_DIR / filename, "JPEG", quality=85)

    # Populate the scaled cache up front so the frame does not wait for a
    # resize the first time it fetches the image.
    scaled_image_path(filename)

    return filename, width, height


def save_generated(img: Image.Image, original_name: str) -> dict:
    filename = uuid.uuid4().hex + ".jpg"
    img.save(DATA_DIR / filename, "JPEG", quality=92, optimize=True)
    thumb = img.copy()
    thumb.thumbnail((400, 400))
    thumb.convert("RGB").save(THUMBS_DIR / filename, "JPEG", quality=85)
    return insert_image(filename, original_name, img.width, img.height)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/server/restart")
def api_restart_server():
    """Re-exec the running uvicorn process after the response has been sent."""

    def restart():
        time.sleep(0.4)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as exc:  # noqa: BLE001
            print(f"MemoryLink server restart failed: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=restart, daemon=True).start()
    return {"ok": True, "restarting": True}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/images/{filename}")
def get_image(filename: str):
    path = safe_filename(filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(scaled_image_path(filename))


@app.get("/thumbs/{filename}")
def get_thumb(filename: str):
    safe_filename(filename)
    thumb = THUMBS_DIR / filename
    if not thumb.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(thumb)


@app.get("/update.apk")
def get_update_apk():
    if not UPDATE_PATH.is_file():
        raise HTTPException(status_code=404, detail="No update has been pushed")
    return FileResponse(
        UPDATE_PATH,
        media_type="application/vnd.android.package-archive",
        filename="memorylink.apk",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/images")
def api_list_images():
    with db_conn() as db:
        return {"images": list_images(db)}


@app.get("/api/tags")
def api_list_tags():
    with db_conn() as db:
        return {"tags": all_tags(db)}


class BluetoothDeviceIn(BaseModel):
    address: str
    name: str | None = None
    rssi: int | None = None


class BluetoothReport(BaseModel):
    devices: list[BluetoothDeviceIn]


@app.post("/api/bluetooth")
def api_bluetooth_report(req: BluetoothReport):
    """Upsert devices seen by the frame's Bluetooth scan."""
    seen = now()
    count = 0
    with db_conn() as db:
        for dev in req.devices:
            address = dev.address.strip().upper()
            if not address or len(address) > 64:
                continue
            name = (dev.name or "").strip()[:200] or None
            db.execute(
                """
                INSERT INTO bluetooth_devices
                    (address, name, rssi, first_seen, last_seen, seen_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(address) DO UPDATE SET
                    name = COALESCE(excluded.name, bluetooth_devices.name),
                    rssi = COALESCE(excluded.rssi, bluetooth_devices.rssi),
                    last_seen = excluded.last_seen,
                    seen_count = bluetooth_devices.seen_count + 1
                """,
                (address, name, dev.rssi, seen, seen),
            )
            count += 1
    return {"ok": True, "count": count}


@app.get("/api/bluetooth")
def api_bluetooth_list():
    with db_conn() as db:
        rows = db.execute(
            "SELECT * FROM bluetooth_devices ORDER BY last_seen DESC"
        ).fetchall()
    return {"devices": [dict(r) for r in rows]}


class BluetoothCommentRequest(BaseModel):
    comment: str = ""


@app.put("/api/bluetooth/{address}/comment")
def api_bluetooth_comment(address: str, req: BluetoothCommentRequest):
    address = address.strip().upper()
    comment = req.comment.strip()[:500]
    with db_conn() as db:
        row = db.execute(
            "SELECT * FROM bluetooth_devices WHERE address = ?", (address,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")
        db.execute(
            "UPDATE bluetooth_devices SET comment = ? WHERE address = ?",
            (comment or None, address),
        )
        row = db.execute(
            "SELECT * FROM bluetooth_devices WHERE address = ?", (address,)
        ).fetchone()
    return {"ok": True, "device": dict(row)}


class SetTagsRequest(BaseModel):
    tags: list[str]


class OrderRequest(BaseModel):
    image_ids: list[int]


@app.put("/api/images/order")
def api_reorder(req: OrderRequest):
    """Replace the gallery order. Must list every image exactly once."""
    ids = req.image_ids
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="image_ids contains duplicates")
    with db_conn() as db:
        existing = {r["id"] for r in db.execute("SELECT id FROM images").fetchall()}
        if set(ids) != existing:
            raise HTTPException(
                status_code=400,
                detail="image_ids must contain every image exactly once",
            )
        for position, image_id in enumerate(ids):
            db.execute(
                "UPDATE images SET position = ? WHERE id = ?", (position, image_id)
            )
        bump_version(db)
    return {"ok": True, "image_ids": ids}


@app.put("/api/images/{image_id}/tags")
def api_set_tags(image_id: int, req: SetTagsRequest):
    with db_conn() as db:
        row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found")
        tags = set_image_tags(db, image_id, req.tags)
        bump_version(db)
    return {"ok": True, "tags": tags}


@app.post("/api/images")
async def api_upload(files: list[UploadFile] = File(...)):
    saved = []
    for uf in files:
        data = await uf.read()
        if not data:
            continue
        filename, width, height = normalize_and_save(data)
        saved.append(insert_image(filename, uf.filename or filename, width, height))
    if not saved:
        raise HTTPException(status_code=400, detail="No readable images uploaded")
    return {"ok": True, "images": saved}


@app.get("/api/update")
def api_get_update():
    with db_conn() as db:
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
    return {"update": update_dict(state)}


def record_update(name: str, sha256: str, size: int) -> dict | None:
    with db_conn() as db:
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        update_id = (state["update_id"] or 0) + 1
        db.execute(
            "UPDATE frame_state SET update_id = ?, update_name = ?, update_sha256 = ?, "
            "update_size = ?, update_uploaded_at = ?, update_status = 'pending', "
            "update_error = NULL, update_result_at = NULL WHERE id = ?",
            (update_id, name, sha256, size, now(), FRAME_ID),
        )
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        return update_dict(state)


GITHUB_API = "https://api.github.com"


class GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Artifact downloads redirect to a signed URL that rejects our token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
            new.remove_header("X-GitHub-Api-Version")
        return new


_github_download_opener = urllib.request.build_opener(GitHubRedirectHandler())


def github_token() -> str | None:
    try:
        token = GITHUB_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MemoryLinkServer",
    }


def github_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers=github_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub API returned HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail="Could not reach GitHub") from exc


def latest_apk_artifact(token: str) -> dict:
    data = github_json(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/actions/artifacts?per_page=100",
        token,
    )
    best: tuple[int, dict] | None = None
    for artifact in data.get("artifacts", []):
        if artifact.get("expired"):
            continue
        name = artifact.get("name") or ""
        if not name.startswith("MemoryLink-") or not name.endswith(".apk"):
            continue
        if "-debug" in name:
            continue
        match = re.search(r"(\d+)\.apk$", name)
        if not match:
            continue
        run = artifact.get("workflow_run") or {}
        branch = run.get("head_branch")
        if branch and branch != "main":
            continue
        build = int(match.group(1))
        if best is None or build > best[0]:
            best = (build, artifact)
    if best is None:
        raise HTTPException(status_code=404, detail="No release APK artifact found")
    return best[1]


def download_github_artifact(artifact: dict, token: str, dest: Path) -> None:
    url = artifact.get("archive_download_url")
    if not url:
        raise HTTPException(status_code=502, detail="Artifact has no download URL")
    request = urllib.request.Request(url, headers=github_headers(token))
    try:
        with _github_download_opener.open(request, timeout=60) as response, dest.open("wb") as out:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_GITHUB_DOWNLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="GitHub artifact is too large")
                out.write(chunk)
    except HTTPException:
        raise
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub artifact download returned HTTP {exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail="Could not download GitHub artifact") from exc


def extract_github_apk(download: Path, dest: Path, artifact: dict) -> tuple[str, Path]:
    name = artifact.get("name") or "update.apk"
    if zipfile.is_zipfile(download):
        with zipfile.ZipFile(download) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".apk")]
            if names:
                entry = max(names, key=lambda n: archive.getinfo(n).file_size)
                info = archive.getinfo(entry)
                if info.file_size > MAX_APK_BYTES:
                    raise HTTPException(
                        status_code=413, detail="APK in artifact is too large"
                    )
                with archive.open(entry) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                return os.path.basename(entry), dest
    return name, download


class GithubTokenRequest(BaseModel):
    token: str = ""


@app.get("/api/github/status")
def api_github_status():
    return {"configured": github_token() is not None, "repo": GITHUB_REPO}


@app.post("/api/github/token")
def api_github_set_token(req: GithubTokenRequest):
    token = req.token.strip()
    if token:
        tmp = GITHUB_TOKEN_PATH.with_name(GITHUB_TOKEN_PATH.name + ".tmp")
        tmp.write_text(token, encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(GITHUB_TOKEN_PATH)
    else:
        GITHUB_TOKEN_PATH.unlink(missing_ok=True)
    return {"ok": True, "configured": bool(token), "repo": GITHUB_REPO}


@app.post("/api/github/download")
def api_github_download():
    token = github_token()
    if not token:
        raise HTTPException(status_code=400, detail="No GitHub token configured")
    artifact = latest_apk_artifact(token)
    download = DATA_DIR / "github_artifact.tmp"
    extracted = DATA_DIR / "github_artifact.apk.tmp"
    try:
        download_github_artifact(artifact, token, download)
        name, apk_path = extract_github_apk(download, extracted, artifact)
        size = apk_path.stat().st_size
        if size <= 0 or size > MAX_APK_BYTES:
            raise HTTPException(status_code=400, detail="Downloaded APK has an invalid size")
        with apk_path.open("rb") as f:
            if not f.read(2).startswith(b"PK"):
                raise HTTPException(status_code=400, detail="Downloaded artifact is not an APK")
        hasher = hashlib.sha256()
        with apk_path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                hasher.update(chunk)
        apk_path.replace(UPDATE_PATH)
        update = record_update(name, hasher.hexdigest(), size)
    finally:
        download.unlink(missing_ok=True)
        extracted.unlink(missing_ok=True)
    return {
        "ok": True,
        "update": update,
        "artifact": {
            "name": artifact.get("name"),
            "id": artifact.get("id"),
            "created_at": artifact.get("created_at"),
        },
    }


@app.post("/api/update")
async def api_push_update(apk: UploadFile = File(...)):
    """Store an APK and announce it to the frame as a new update."""
    tmp = UPDATE_PATH.with_name(UPDATE_PATH.name + ".tmp")
    hasher = hashlib.sha256()
    size = 0
    first_bytes = b""
    try:
        with tmp.open("wb") as out:
            while chunk := await apk.read(1024 * 1024):
                if not first_bytes:
                    first_bytes = chunk[:4]
                size += len(chunk)
                if size > MAX_APK_BYTES:
                    raise HTTPException(status_code=400, detail="APK is too large")
                hasher.update(chunk)
                out.write(chunk)
        if not first_bytes.startswith(b"PK"):
            raise HTTPException(status_code=400, detail="Not an APK file")
        tmp.replace(UPDATE_PATH)
    finally:
        tmp.unlink(missing_ok=True)

    update = record_update(apk.filename or "update.apk", hasher.hexdigest(), size)
    return {"ok": True, "update": update}


class UpdateResultRequest(BaseModel):
    update_id: int
    status: str
    message: str | None = None


@app.post("/api/frame/update-result")
def api_update_result(req: UpdateResultRequest):
    """Record the install state reported by the frame."""
    if req.status not in ("installing", "installed", "failed", "canceled"):
        raise HTTPException(status_code=400, detail="Invalid update status")
    with db_conn() as db:
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        if not state["update_id"] or state["update_id"] != req.update_id:
            raise HTTPException(status_code=409, detail="Not the current update")
        error = (req.message or "")[:500] if req.status == "failed" else None
        db.execute(
            "UPDATE frame_state SET update_status = ?, update_error = ?, "
            "update_result_at = ? WHERE id = ?",
            (req.status, error, now(), FRAME_ID),
        )
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        update = update_dict(state)
    return {"ok": True, "update": update}


@app.post("/api/update/cancel")
def api_cancel_update():
    """Withdraw the queued APK so the frame will not install it."""
    with db_conn() as db:
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        if not state["update_id"]:
            raise HTTPException(status_code=404, detail="No update to cancel")
        db.execute(
            "UPDATE frame_state SET update_status = 'canceled', update_error = NULL, "
            "update_result_at = ? WHERE id = ?",
            (now(), FRAME_ID),
        )
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        update = update_dict(state)
    return {"ok": True, "update": update}


@app.delete("/api/images/{image_id}")
def api_delete(image_id: int):
    with db_conn() as db:
        row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found")
        db.execute("DELETE FROM images WHERE id = ?", (image_id,))
        db.execute(
            "UPDATE frame_state SET current_image_id = NULL, mode = 'slideshow' "
            "WHERE id = ? AND current_image_id = ?",
            (FRAME_ID, image_id),
        )
        db.execute(
            "UPDATE frame_state SET shown_image_id = NULL, shown_at = NULL "
            "WHERE id = ? AND shown_image_id = ?",
            (FRAME_ID, image_id),
        )
        bump_version(db)
        filename = row["filename"]

    for path in (DATA_DIR / filename, THUMBS_DIR / filename, SCALED_DIR / filename):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@app.post("/api/images/{image_id}/display")
def api_display(image_id: int):
    with db_conn() as db:
        row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found")
        db.execute(
            "UPDATE frame_state SET mode = 'single', current_image_id = ?, "
            "version = version + 1 WHERE id = ?",
            (image_id, FRAME_ID),
        )
    return {"ok": True, "mode": "single", "image_id": image_id}


class SlideshowRequest(BaseModel):
    interval_seconds: int = DEFAULT_INTERVAL
    tags: list[str] = []
    exclude_tags: list[str] = []
    match_all: bool = True
    random: bool = False


@app.post("/api/frame/slideshow")
def api_slideshow(req: SlideshowRequest):
    interval = max(5, min(3600, req.interval_seconds))
    tags: list[str] = []
    for t in req.tags:
        name = normalize_tag(t)
        if name and name not in tags:
            tags.append(name)
    excludes: list[str] = []
    for t in req.exclude_tags:
        name = normalize_tag(t)
        if name and name not in excludes:
            excludes.append(name)
    with db_conn() as db:
        db.execute(
            "UPDATE frame_state SET mode = 'slideshow', interval_seconds = ?, "
            "tag_filter = ?, exclude_tags = ?, match_all = ?, random = ?, "
            "version = version + 1 WHERE id = ?",
            (
                interval,
                json.dumps(tags),
                json.dumps(excludes),
                1 if req.match_all else 0,
                1 if req.random else 0,
                FRAME_ID,
            ),
        )
    return {
        "ok": True,
        "mode": "slideshow",
        "interval_seconds": interval,
        "tags": tags,
        "exclude_tags": excludes,
        "match_all": req.match_all,
        "random": req.random,
    }


class TransitionsRequest(BaseModel):
    transitions: list[str]


@app.put("/api/frame/transitions")
def api_set_transitions(req: TransitionsRequest):
    """Choose which transition effects the frame may use (empty = all)."""
    selected: list[str] = []
    for raw in req.transitions:
        name = raw.strip().upper()
        if name in TRANSITIONS and name not in selected:
            selected.append(name)
    with db_conn() as db:
        db.execute(
            "UPDATE frame_state SET transitions = ? WHERE id = ?",
            (json.dumps(selected), FRAME_ID),
        )
    return {"ok": True, "transitions": selected}


class ClockRequest(BaseModel):
    show_clock: bool


@app.put("/api/frame/clock")
def api_set_clock(req: ClockRequest):
    with db_conn() as db:
        db.execute(
            "UPDATE frame_state SET show_clock = ? WHERE id = ?",
            (1 if req.show_clock else 0, FRAME_ID),
        )
    return {"ok": True, "show_clock": req.show_clock}


class BlurRequest(BaseModel):
    blur_background: bool


@app.put("/api/frame/blur")
def api_set_blur(req: BlurRequest):
    with db_conn() as db:
        db.execute(
            "UPDATE frame_state SET blur_background = ? WHERE id = ?",
            (1 if req.blur_background else 0, FRAME_ID),
        )
    return {"ok": True, "blur_background": req.blur_background}


def build_frame_state() -> dict:
    with db_conn() as db:
        state = db.execute("SELECT * FROM frame_state WHERE id = ?", (FRAME_ID,)).fetchone()
        tag_filter = json.loads(state["tag_filter"] or "[]")
        exclude_tags = json.loads(state["exclude_tags"] or "[]")
        match_all = bool(state["match_all"])
        transitions = json.loads(state["transitions"] or "[]")
        images = list_images(db, tag_filter or None, match_all, exclude_tags or None)

    mode = state["mode"]
    single = None
    if mode == "single" and state["current_image_id"] is not None:
        with db_conn() as db:
            row = db.execute(
                "SELECT * FROM images WHERE id = ?", (state["current_image_id"],)
            ).fetchone()
            if row is None:
                mode = "slideshow"
            else:
                single = image_dict(db, row)

    return {
        "mode": mode,
        "version": state["version"],
        "interval_seconds": state["interval_seconds"],
        "image": single,
        "images": images,
        "filter": {
            "tags": tag_filter,
            "exclude_tags": exclude_tags,
            "match_all": match_all,
        },
        "random": bool(state["random"]),
        "show_clock": bool(state["show_clock"]),
        "blur_background": bool(state["blur_background"]),
        "transitions": transitions,
        "shown_image_id": state["shown_image_id"],
        "shown_at": state["shown_at"],
        "last_seen_at": state["last_seen_at"],
        "restart_id": state["restart_id"],
        "online": frame_online(state),
        "update": update_dict(state),
        "version_info": version_info(state),
    }


@app.get("/api/frame/current")
def api_frame_current():
    return build_frame_state()


@app.get("/api/frame/wait")
def api_frame_wait(
    version: int,
    update_id: int = 0,
    update_status: str | None = None,
    show_clock: bool | None = None,
    blur_background: bool | None = None,
    restart_id: int | None = None,
    timeout: int = 15,
):
    """Long poll: return as soon as the frame state changes.

    The frame stays online while waiting because this endpoint refreshes
    last_seen_at periodically. On timeout it responds with 204 so the client
    simply reconnects.
    """
    timeout = max(1, min(30, timeout))
    deadline = time.monotonic() + timeout
    last_ping = 0.0
    while True:
        now_mono = time.monotonic()
        changed = False
        with db_conn() as db:
            state = db.execute(
                "SELECT version, update_id, update_status, show_clock, "
                "blur_background, restart_id FROM frame_state WHERE id = ?",
                (FRAME_ID,),
            ).fetchone()
            current_status = (
                (state["update_status"] or "pending")
                if (state["update_id"] or 0)
                else ""
            )
            changed = (
                state["version"] != version
                or (state["update_id"] or 0) != update_id
            )
            if update_status is not None:
                changed = changed or current_status != update_status
            if show_clock is not None:
                changed = changed or bool(state["show_clock"]) != show_clock
            if blur_background is not None:
                changed = changed or bool(state["blur_background"]) != blur_background
            if restart_id is not None:
                changed = changed or (state["restart_id"] or 0) != restart_id
            if not changed and now_mono - last_ping >= 5:
                db.execute(
                    "UPDATE frame_state SET last_seen_at = ? WHERE id = ?",
                    (now(), FRAME_ID),
                )
                last_ping = now_mono
        if changed:
            return build_frame_state()
        if now_mono >= deadline:
            break
        time.sleep(0.5)
    return Response(status_code=204)


class ShownRequest(BaseModel):
    image_id: int


@app.get("/api/frame/status")
def api_frame_status():
    """Lightweight state for the web UI to show what the frame reported."""
    with db_conn() as db:
        state = db.execute(
            "SELECT * FROM frame_state WHERE id = ?",
            (FRAME_ID,),
        ).fetchone()
    return {
        "mode": state["mode"],
        "shown_image_id": state["shown_image_id"],
        "shown_at": state["shown_at"],
        "last_seen_at": state["last_seen_at"],
        "restart_id": state["restart_id"],
        "online": frame_online(state),
        "update": update_dict(state),
        "version_info": version_info(state),
    }


@app.post("/api/frame/restart")
def api_frame_restart():
    """Ask the frame app to restart itself on its next poll."""
    with db_conn() as db:
        db.execute(
            "UPDATE frame_state SET restart_id = restart_id + 1 WHERE id = ?",
            (FRAME_ID,),
        )
        state = db.execute(
            "SELECT restart_id FROM frame_state WHERE id = ?", (FRAME_ID,)
        ).fetchone()
    return {"ok": True, "restart_id": state["restart_id"]}


@app.post("/api/frame/heartbeat")
def api_frame_heartbeat(
    version_code: int | None = None,
    version_name: str | None = None,
):
    """Mark the frame as alive; called on every poll."""
    with db_conn() as db:
        if version_code is not None:
            db.execute(
                "UPDATE frame_state SET last_seen_at = ?, frame_version_code = ?, "
                "frame_version_name = ?, frame_version_at = ? WHERE id = ?",
                (now(), version_code, version_name, now(), FRAME_ID),
            )
        else:
            db.execute(
                "UPDATE frame_state SET last_seen_at = ? WHERE id = ?",
                (now(), FRAME_ID),
            )
    return {"ok": True}


@app.post("/api/frame/shown")
def api_frame_shown(req: ShownRequest):
    """Record which image the frame is actually displaying.

    This deliberately does not bump the frame version: reporting must not make
    the frame reload its slideshow.
    """
    with db_conn() as db:
        row = db.execute("SELECT id FROM images WHERE id = ?", (req.image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found")
        db.execute(
            "UPDATE frame_state SET shown_image_id = ?, shown_at = ?, "
            "last_seen_at = ? WHERE id = ?",
            (req.image_id, now(), now(), FRAME_ID),
        )
    return {"ok": True}


class CombineRequest(BaseModel):
    image_ids: list[int]


@app.post("/api/edit/combine")
def api_combine(req: CombineRequest):
    """Stack several (typically 3 landscape) images into one 1080x1920 portrait."""
    ids = list(dict.fromkeys(req.image_ids))
    if not (1 <= len(ids) <= 12):
        raise HTTPException(status_code=400, detail="Select between 1 and 12 images")

    paths = []
    with db_conn() as db:
        for iid in ids:
            row = db.execute("SELECT * FROM images WHERE id = ?", (iid,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Image {iid} not found")
            paths.append(DATA_DIR / row["filename"])

    cell_h = FRAME_H // len(paths)
    canvas = Image.new("RGB", (FRAME_W, FRAME_H), "black")
    y = 0
    for path in paths:
        img = Image.open(path).convert("RGB")
        cell = ImageOps.fit(img, (FRAME_W, cell_h), method=Image.LANCZOS, centering=(0.5, 0.5))
        canvas.paste(cell, (0, y))
        y += cell_h

    return {"ok": True, "image": save_generated(canvas, f"combined-{len(ids)}.jpg")}


class FitRequest(BaseModel):
    image_id: int


@app.post("/api/edit/fit")
def api_fit(req: FitRequest):
    """Cover-crop a single image to fill the 1080x1920 frame."""
    with db_conn() as db:
        row = db.execute("SELECT * FROM images WHERE id = ?", (req.image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Image {req.image_id} not found")
        path = DATA_DIR / row["filename"]
        name = row["original_name"]

    img = Image.open(path).convert("RGB")
    out = ImageOps.fit(img, (FRAME_W, FRAME_H), method=Image.LANCZOS, centering=(0.5, 0.5))
    return {"ok": True, "image": save_generated(out, f"{name}-fit.jpg")}


init_db()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
