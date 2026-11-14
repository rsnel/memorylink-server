# MemoryLink server

FastAPI service + single-page web UI for one or more frames. Data lives in
`server/data/` by default (uploaded images, thumbnails, scaled copies and a
SQLite database); set `MEMORYLINK_DATA_DIR` to use another location.

Each frame generates a random UUID on first launch and sends it as `frame_id`
on every frame-specific request. The web UI's frame picker shows a friendly
name when one has been set, otherwise the first eight characters of the UUID.
The first real frame adopts the legacy `default` row so an existing
single-frame installation keeps its settings.

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

For a production installation with a systemd unit, dedicated data directory and
Apache TLS reverse proxy, see [`deploy/README.md`](deploy/README.md).

## Web UI

The page has three parts: a header with the frame picker, the slideshow
controls and the tag filter; the shared photo gallery; and a **Settings** panel
for everything that is set once and left alone. The gallery, tags and order are
shared by all frames; the slideshow, filter, display state, restart command and
update status are per frame.

**Photos**

- **Upload photos** — multi-select or drop files anywhere on the page; accepts
  common formats (JPEG/PNG/WebP/GIF/BMP). The new photos are selected right
  after the upload so they can be tagged in one go. Files that are not images
  are skipped and named in the message.
- **Selecting** — click a photo to select it, Shift-click for a range, or use
  *Select all* / *Select untagged*. Esc deselects. A bar at the bottom then
  offers the actions for the selection:
  - **Add tag** — add one or more tags (comma separated) to every selected photo.
  - **Remove tag…** — take a tag off every selected photo.
  - **Combine into portrait** — stacks the selected photos (typically 3
    landscape shots, in gallery order) into one 1080×1920 portrait.
  - **Crop to portrait** — cover-crops a single photo to 1080×1920.
  - **Delete** — deletes all selected photos.
- **Tags on one photo** — `+ tag` on a card adds tags, `×` removes one. Tag
  names are normalised to lowercase.
- **Show on frame** — pins that photo on the selected frame immediately.
- **Order** — drag photos in the gallery to set the slideshow order. New uploads
  are appended at the end. With a tag filter active, dragging only rearranges the
  visible photos and leaves the hidden ones in place.

**Slideshow and filter**

- **Filter** — click a tag to show only photos with that tag, click it again to
  hide photos with that tag, and a third time to stop filtering on it. With
  several tags included, choose whether a photo needs all of them or any of
  them. Hiding always wins.
- **Start slideshow** — sends the filter, the interval and *Random order* to
  the selected frame. The text next to the button tells what the frame is
  playing, and turns yellow when the page shows something else than the frame
  was told.
- **Frame picker** — next to the title: choose which frame the controls apply
  to. An unnamed frame is shown by the first eight characters of its UUID.

**Settings**

- **This frame** — **Save name** stores a friendly name for the selected frame.
  **Remove frame** deletes an offline frame row (the shared photos, tags and
  APK are kept); online frames must be stopped first.
- **Display** — clock/date overlay; how photos that don't have the shape of the
  screen are shown (zoomed in to fill, whole with a black border, whole on a
  blurred zoomed border, or zoomed in and slowly panning across); and the
  transition effects the frame may use (none ticked = all of them).
- **New uploads** — tags that every uploaded photo gets automatically, for
  example `new, {year}`. `{year}`, `{month}` and `{date}` are replaced by the
  upload date (`2026`, `2026-11`, `2026-11-13`).
- **Tags** — rename a tag everywhere (renaming onto an existing tag merges the
  two) or remove it from all photos. A running slideshow filter follows along.
- **Frame software** — push a signed APK to all known frames, or store a
  GitHub token, download the latest release APK artifact and then push that
  downloaded build separately. Updates are global to the frame fleet; each
  frame reports its own pending/installing/installed/failed status. **Cancel
  update** withdraws the update for every frame that has it. **Restart frame**
  asks the selected frame app to restart itself (settings and image cache are
  preserved).
- **Nearby Bluetooth devices** — BLE devices reported by the frame; each has an
  owner/comment field. The frame scans for 10 seconds every 2 minutes while it
  is running.

Edited images are saved as new library images (carrying the tags their sources
have in common), so they can be shown, deleted,
and combined again like any other.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | liveness check |
| GET | `/api/frames` | list frames (`id`, friendly `name`, `short_id`, `display_name`, `online`) |
| PUT | `/api/frames/{frame_id}` | `{name}` → set or clear a frame's friendly name |
| DELETE | `/api/frames/{frame_id}` | remove an offline frame (optional `?force=true` for an online one); photos are kept |
| POST | `/api/server/restart` | re-exec the running uvicorn process |
| GET | `/api/images` | list images in gallery order (each carries its `tags`) |
| POST | `/api/images` | upload (multipart, field `files`, multiple allowed; optional field `tags`, comma separated). New images also get the configured upload tags |
| PUT | `/api/images/order` | `{image_ids: []}` → set gallery order (every image exactly once) |
| DELETE | `/api/images/{id}` | delete an image |
| POST | `/api/images/delete` | `{image_ids: []}` → delete several images |
| GET | `/api/tags` | list all tags with counts |
| PUT | `/api/images/{id}/tags` | `{tags: []}` → replace an image's tags |
| POST | `/api/images/tags` | `{image_ids: [], add: [], remove: []}` → add/remove tags on a group of images |
| POST | `/api/tags/rename` | `{name, new_name}` → rename a tag everywhere (merges if `new_name` exists) |
| POST | `/api/tags/delete` | `{name}` → remove a tag from every image |
| GET | `/api/settings` | `{upload_tags, upload_tags_now}` — the configured upload tags and what they expand to today |
| PUT | `/api/settings/upload-tags` | `{tags: []}` → tags for every new upload; `{year}`, `{month}`, `{date}` are filled in |
| POST | `/api/bluetooth` | `{devices: [{address, name, rssi}]}` → upsert devices seen by the frame |
| GET | `/api/bluetooth` | list known Bluetooth devices and comments |
| PUT | `/api/bluetooth/{address}/comment` | `{comment}` → set owner/comment for a device |
| GET | `/api/update` | metadata/status of the APK currently queued for the selected frame |
| POST | `/api/update` | upload an APK (multipart, field `apk`) and queue it for all frames |
| POST | `/api/update/cancel` | withdraw the queued APK for all frames |
| GET | `/api/github/status` | token/repo status and the currently downloaded build |
| POST | `/api/github/token` | `{token}` → store (mode 600) or clear with an empty token |
| POST | `/api/github/download` | download the latest release APK artifact into staging (does not queue it) |
| POST | `/api/github/push` | queue the staged download as the frame update |
| POST | `/api/frame/update-result` | `{update_id, status, message}` → record installing/installed/failed for a frame |
| GET | `/update.apk` | download the queued APK |
| GET | `/images/{filename}` | full image; scaled down to at most 1080×1920 and cached in `data/scaled/` |
| GET | `/thumbs/{filename}` | thumbnail |
| POST | `/api/images/{id}/display` | pin an image on the selected frame |
| POST | `/api/frame/slideshow` | `{interval_seconds, tags, exclude_tags, match_all, random}` → filtered slideshow |
| GET | `/api/frame/current` | what the selected frame should show now (`frame_id`, `images`, `library` (every image, `{id, url, tags}` in gallery order, for browsing and offline use), `filter`, `random`, `transitions`, `display_mode`, `blur_background`, `shown_image_id`, `online`, `version_info`) |
| GET | `/api/frame/wait` | long poll; returns the new state when the frame version, update, clock, display mode (`display_mode=`) or blur setting changes (204 on timeout) |
| GET | `/api/frame/status` | `{frame_id, mode, version, current_image_id, interval_seconds, filter, random, shown_image_id, shown_at, last_seen_at, online, update, version_info}` |
| PUT | `/api/frame/transitions` | `{transitions: []}` → choose allowed effects (`[]` = all) |
| PUT | `/api/frame/clock` | `{show_clock: bool}` → show/hide the time overlay |
| PUT | `/api/frame/display` | `{display_mode}`: `fill`, `fit`, `blur` or `pan` |
| PUT | `/api/frame/blur` | older form: `{blur_background: bool}` → `blur` or `fit` |
| POST | `/api/frame/heartbeat` | `?version_code=…&version_name=…` → mark frame alive and report its build |
| POST | `/api/frame/restart` | increment the frame restart command id |
| POST | `/api/frame/shown` | `{image_id}` → record which image the frame is actually displaying |
| POST | `/api/edit/combine` | `{image_ids: []}` → 1080×1920 stack |
| POST | `/api/edit/fit` | `{image_id}` → 1080×1920 cover-crop |

Frame-specific endpoints accept `?frame_id=<uuid>`; omitting it addresses the
legacy `default` frame. The Android app generates the UUID once and keeps it
across updates; the configuration screen shows the full id. Shared library
changes (upload/delete/tags/order) bump every frame's version, while
display/slideshow/restart/update-status changes are scoped to one frame.

The tag filter lives entirely on the server, so `GET /api/frame/current` already
returns the filtered image list that the selected frame cycles through. The
list is ordered by the gallery order; the frame displays it in that order unless
the slideshow was started with `random: true`, in which case it picks each next
image at random (never the one already on screen). The frame reports what it
actually shows via `POST /api/frame/shown`, which the web UI shows as a green
“On frame” badge for the selected frame. The app also keeps downloaded images
in its local cache directory, so a restart does not re-download the library.

`GET /api/frame/current` also includes an `update` object after an APK has been
pushed. A pushed update is queued for every known frame and the APK file is
shared; each frame only installs it if it is signed by the same key as the
installed app. Android shows an install confirmation on the device. Installing
from the app requires **Install unknown apps** to be enabled once (on Android 7
and older: **Settings → Security → Unknown sources**). Each frame reports
`installing`, `installed` or `failed` back to the server, and the web UI
refreshes the selected frame's status every few seconds. Once the installer
dialog is open, canceling has to be done on the frame; **Cancel update**
prevents every frame from starting or continuing the queued install.

The frame calls `POST /api/frame/heartbeat` on every poll and keeps a
`GET /api/frame/wait` long poll open. Commands from the web UI (show an image,
start a slideshow, change the order, push an update) therefore reach the frame
immediately instead of waiting for the next poll. The long poll also refreshes
the heartbeat while it waits. The web UI shows **Frame online** / **Frame
offline** and hides the “On frame” marker when no heartbeat has arrived for 20
seconds. Older app builds don't send heartbeats or use the long poll and will
therefore appear offline.

The Frame software settings's **Restart frame** button increments `restart_id`; the long
poll returns immediately, the app persists the handled id, and recreates its
activity. The splash, frame loop and Bluetooth scanner are restarted without
losing the server URL or cached photos.

The web UI has no authentication, so keep it behind your reverse proxy's access
control: anyone who can reach it can upload images and queue APKs.

## Version recommendations

The status payload includes `version_info`, and the web UI shows a warning when
it detects drift:

- **Frame behind** — the frame reports its `versionCode` in the heartbeat. The
  server extracts the build number from a queued APK named
  `MemoryLink-<build>.apk`; if the queued build is newer than the frame, the page
  recommends pushing/approving the update.
- **Server behind** — `SERVER_VERSION` in `app.py` is the version of the running
  process, while `VERSION` is the deployment marker on disk. Bump both whenever
  you change the server code. If the file is newer than the running constant,
  the page recommends restarting the server.
- **Restarting** — the recommendation (and the Frame software settings) has a **Restart
  server** button that calls `POST /api/server/restart`. The server responds,
  then re-execs uvicorn with its original command line and environment.
  This assumes a simple single-process uvicorn command; the first deployment
  that adds the endpoint still needs a manual restart, after which the button
  can apply future server updates.

CI builds use their workflow run number as `versionCode`, so the artifact name
and the frame build number line up with this check.

## Bluetooth scanning

While the frame is running it performs a BLE scan for 10 seconds every 2
minutes and posts the address, name and RSSI of each device to
`POST /api/bluetooth`. The server keeps first/last-seen timestamps, a
`seen_count` that increments once per scan cycle in which the address appears,
and a free-text comment per address; the web UI's **Bluetooth** panel shows the
counter and lets you edit the owner/comment. On Android 7–11 the frame needs
location permission and
location services enabled for BLE scanning; Android 12+ uses the Nearby devices
permission.

## Downloading builds from GitHub Actions

The **GitHub** panel can fetch the newest release artifact without downloading it
manually. Supply a token with access to Actions artifacts (a classic token with
`repo` scope, or a fine-grained token with **Actions: Read**), save it, and press
**Download latest build**. This only stages the APK on the server; it does not
touch the frame. The separate **Push downloaded build** button in the frame
software section then queues it through the normal update flow. The token is
stored in `data/github_token` with mode `600` and is never sent back to the
browser. **Clear** removes it.

The server looks at `rsnel/memorylink-app` (override with
`MEMORYLINK_GITHUB_REPO`), selects the highest-numbered non-expired
`MemoryLink-<build>.apk` release artifact on `main`, and downloads it. Debug
artifacts are ignored. GitHub's artifact download redirects to a signed URL, so
the Authorization header is stripped on that redirect.

Since the web UI is unauthenticated, keep it behind your reverse proxy's access
control; anyone who can reach it can use the stored token or replace it.

## HTTPS

The Android app is meant to be configured with an HTTPS URL only. Point a
reverse proxy at the uvicorn process. Example `Caddyfile` (Caddy terminates TLS
and issues certificates automatically):

```
frame.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then enter `https://frame.example.com` in the app.
