# Deploying MemoryLink on Linux

This directory contains a systemd unit, an Apache 2.4 reverse-proxy example and
an installer for a Debian/Ubuntu-style host. The server itself is a FastAPI app
served by uvicorn; Apache terminates TLS and proxies to loopback.

## Storage locations

| Path | Purpose |
| --- | --- |
| `/opt/memorylink/server` | Application code and its virtualenv |
| `/var/lib/memorylink` | Database, uploaded images, thumbnails, scaled cache, queued APK and GitHub token |
| `/etc/memorylink.env` | Environment overrides (data path, optional GitHub repo) |
| `/etc/systemd/system/memorylink.service` | systemd unit |
| `/etc/apache2/sites-available/memorylink.conf` | Apache vhost example |
| journald | Service logs (`journalctl -u memorylink`) |

`MEMORYLINK_DATA_DIR` selects the data directory. The unit sets it to
`/var/lib/memorylink`; `/etc/memorylink.env` can override it, but if you change
it, update `ReadWritePaths=` in the unit too.

The data directory is owned by the dedicated `memorylink` user and mode `0750`.
The GitHub token is a `0600` file inside it. Back up `/var/lib/memorylink`; the
code and virtualenv can be rebuilt from git.

## Install

On Debian/Ubuntu, install the prerequisites:

```bash
apt install python3 python3-venv apache2 certbot python3-certbot-apache
```

Clone the server repository and run the installer as root:

```bash
git clone https://github.com/rsnel/memorylink-server.git
cd memorylink-server
sudo deploy/install.sh
```

The installer creates the `memorylink` user and `/var/lib/memorylink`, copies
the code to `/opt/memorylink/server`, creates the virtualenv, installs
`requirements.txt`, installs the unit and enables it. It does **not** start the
service or touch `/var/lib/memorylink` on upgrades.

Review `/etc/memorylink.env`, then start it:

```bash
systemctl start memorylink
systemctl status memorylink
curl http://127.0.0.1:8000/api/health
```

## Apache reverse proxy

```bash
a2enmod proxy proxy_http ssl headers
cp deploy/apache2-memorylink.conf /etc/apache2/sites-available/memorylink.conf
# edit ServerName and certificate paths
a2ensite memorylink
apache2ctl configtest
systemctl reload apache2
```

Obtain a certificate first, for example:

```bash
certbot certonly --standalone -d frame.example.com
```

The example config enables HSTS, a 75-second proxy timeout for the frame's
15-second long polls, and a 250 MB request limit for APK/image uploads. It also
contains an optional HTTP Basic auth block; if enabled, use
`https://user:password@frame.example.com` as the app's server URL.

Only ports 80/443 need to be reachable from outside. Uvicorn stays bound to
`127.0.0.1:8000`.

## Upgrades

Pull the repository and rerun the installer:

```bash
git pull
sudo deploy/install.sh
sudo systemctl restart memorylink
```

Alternatively, with the web UI's **Restart server** button, you can replace the
code first and then restart without shell access. Re-running the installer
preserves `/var/lib/memorylink`.

The installer replaces the code and virtualenv while the old process may still
be running; restart the service afterwards. The server's own restart button
re-execs the uvicorn process and reads the new code.

## Security notes

- Run uvicorn as the unprivileged `memorylink` user; it needs no capabilities to
  bind port 8000.
- The web UI is unauthenticated. Restrict access with Apache Basic auth, a VPN
  or firewall rules.
- The GitHub token is stored server-side in `data/github_token` (mode 600);
  `.gitignore` excludes the data directory, so it never enters git.
- The unit uses `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges` and an
  empty capability set. If you relocate the code or data, adjust
  `WorkingDirectory=`, `ReadWritePaths=` and the venv path.
