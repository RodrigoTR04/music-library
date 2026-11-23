#!/usr/bin/env python3
"""Music Library: browse the tags and cover art of your own music folders in the browser.

    python3 server.py ~/Music                      # scan the folder (first run), then serve it
    python3 server.py ~/Music Games=/mnt/ost       # several folders, optionally named LABEL=PATH
    python3 server.py                              # serve the folders saved last time; with none, open
                                                   # http://127.0.0.1:8766 and choose them in the browser

Options
    --rescan       scan again even though the folders have not changed
    --port 8766    --host 127.0.0.1    --data ./data (database and saved folder list)

Nothing in your music folders is ever written to. The database is a separate file in the data folder.

Endpoints
    /                    the library      /setup               choose folders, scan
    /api/library         artists, albums, genres, years, flags, stats and light track rows
    /api/track/<id>      one track: all columns, lyrics, raw tags
    /cover/<hash>.jpg    cover thumbnail
"""
import argparse
import json
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import scan
import tags

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}

TRACK_COLS = """id, source, album_id, artist_id, album_pos, flags, search, filename, file_size,
    duration, bitrate, title, artist, album, track_no, disc_no, disc_total, year, genre, composer,
    bpm, cover_hash, (lyrics IS NOT NULL) AS has_lyrics, lyrics_synced, (mb_track_id IS NOT NULL) AS has_mb"""


class Scanner:
    """Runs one scan at a time in a background thread and reports how far it has got."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.state = {"state": "idle"}

    def status(self):
        return dict(self.state)

    def start(self, roots):
        with self.lock:
            if self.state["state"] == "running":
                return False
            self.state = {"state": "running", "phase": "finding", "done": 0, "total": 0, "started": time.time(),
                          "roots": [r["label"] for r in roots]}
        threading.Thread(target=self._run, args=(roots,), daemon=True).start()
        return True

    def _run(self, roots):
        def progress(phase, done, total):
            self.state.update(phase=phase, done=done, total=total)
        try:
            res = scan.scan(roots, config.db_path(self.app.data), progress)
        except scan.ScanError as e:
            self.state = {"state": "error", "message": str(e)}
        except Exception as e:                      # keep the server alive whatever the scan hits
            self.state = {"state": "error", "message": f"{type(e).__name__}: {e}"}
        else:
            self.app.reload()
            self.state = {"state": "done", "tracks": res["tracks"], "albums": res["albums"], "artists": res["artists"],
                          "seconds": res["seconds"], "unreadable": [{"path": p, "error": e} for p, e in res["errors"][:50]],
                          "n_unreadable": len(res["errors"])}


class App:
    def __init__(self, data, setup_enabled):
        self.data = Path(data)
        self.setup_enabled = setup_enabled
        self.local = threading.local()
        self.generation = 0
        self._library = None
        self.scanner = Scanner(self)

    # -- database
    def reload(self):
        self._library = None
        self.generation += 1

    def db(self):
        loc = self.local
        if getattr(loc, "gen", None) != self.generation:
            if getattr(loc, "db", None):
                loc.db.close()
            loc.db = sqlite3.connect(f"file:{config.db_path(self.data)}?mode=ro", uri=True)
            loc.db.row_factory = sqlite3.Row
            loc.gen = self.generation
        return loc.db

    def has_library(self):
        """A database exists and was written by this version of the scanner."""
        p = config.db_path(self.data)
        if not p.exists():
            return False
        try:
            return self.db().execute("PRAGMA user_version").fetchone()[0] == scan.VERSION
        except sqlite3.Error:
            return False

    # -- data for the UI
    def library(self):
        """Everything the UI needs, read straight from the derived tables."""
        if self._library is None:
            q = lambda sql: [dict(r) for r in self.db().execute(sql)]
            unjson = lambda rows, *keys: [{**r, **{k: json.loads(r[k]) for k in keys}} for r in rows]
            payload = {
                "stats": {r["key"]: r["value"] for r in q("SELECT key, value FROM stats")},
                "sources": q("SELECT label, n_tracks, n_albums, seconds, bytes FROM sources ORDER BY position"),
                "flags": q("SELECT bit, key, label, n_tracks FROM flags ORDER BY bit"),
                "genres": unjson(q("SELECT name, n_tracks, n_albums, album_ids FROM genres"), "album_ids"),
                "years": unjson(q("SELECT year, n_tracks, n_albums, album_ids FROM years ORDER BY year"), "album_ids"),
                "artists": unjson(q("SELECT id, source, name, n_albums, n_tracks, duration, covers, album_ids FROM artists"),
                                  "covers", "album_ids"),
                "albums": unjson(q("""SELECT id, artist_id, source, folder, title, album_artist, year,
                                      n_tracks, duration, file_size, mtime, cover_hash, color, copyright, label, genres, track_ids
                                      FROM albums"""), "genres", "track_ids"),
                "tracks": q(f"SELECT {TRACK_COLS} FROM tracks ORDER BY id"),
            }
            genres = {}
            for r in self.db().execute("SELECT tg.track_id, g.name FROM track_genres tg JOIN genres g ON g.id = tg.genre_id"):
                genres.setdefault(r[0], []).append(r[1])
            for t in payload["tracks"]:
                t["genres"] = genres.get(t["id"], [])
                if not t["title"]:                  # untagged: show the file name
                    t["title"] = Path(t["filename"] or "").stem or None
            self._library = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return self._library

    def track(self, tid):
        r = self.db().execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return None
        out = dict(r)
        out["raw"] = [list(x) for x in self.db().execute(
            "SELECT frame, value FROM tags_raw WHERE track_id=? ORDER BY rowid", (tid,))]
        return out

    def cover(self, h):
        r = self.db().execute("SELECT mime, data FROM covers WHERE hash=?", (h,)).fetchone()
        return (r["mime"], r["data"]) if r else None

    # -- setup
    def config(self):
        roots = config.load(self.data).get("roots", [])
        counts = {}
        if self.has_library():
            counts = {r["label"]: r["n_tracks"] for r in self.db().execute("SELECT label, n_tracks FROM sources")}
        return {"roots": [{**r, "n_tracks": counts.get(r["label"])} for r in roots],
                "has_library": self.has_library(), "scan": self.scanner.status(),
                "home": str(Path.home()), "formats": sorted(e[1:] for e in tags.AUDIO_EXTS)}


def list_dir(path):
    """Subfolders of a directory, for the folder picker."""
    p = Path(path or Path.home()).expanduser()
    if not p.is_dir():
        raise ValueError(f"{p} is not a folder.")
    p = p.resolve()
    dirs, audio, note = [], 0, None
    try:
        for e in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            try:
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    dirs.append({"name": e.name, "path": str(e)})
                elif e.suffix.lower() in tags.AUDIO_EXTS:
                    audio += 1
            except OSError:
                continue
    except PermissionError:
        note = "This folder cannot be read."
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs[:2000],
            "audio": audio, "note": note, "home": str(Path.home())}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_bytes(self, body, ctype, status=200, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, obj, status=200):
            body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_bytes(body, "application/json; charset=utf-8", status, {"Cache-Control": "no-cache"})

        def page(self, name):
            self.send_bytes((WEB / name).read_bytes(), "text/html; charset=utf-8", extra={"Cache-Control": "no-cache"})

        def redirect(self, where):
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def trusted(self):
            """Refuse requests that did not come from a page this server served. A site you happen to
            have open could otherwise post to localhost, or reach it through DNS rebinding."""
            h = self.headers.get("Host") or ""
            host = h[:h.index("]") + 1] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0]
            if app.loopback_only and host not in LOOPBACK:
                return False
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        def body_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if 0 <= n < 1_000_000 else None
            except ValueError:
                return None

        do_HEAD = lambda self: self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if not self.trusted():
                return self.send_json({"error": "forbidden"}, 403)
            try:
                if path in ("/", "/index.html"):
                    return self.page("index.html") if app.has_library() else self.redirect("/setup")
                if path == "/setup":
                    return self.page("setup.html")
                if m := re.fullmatch(r"/fonts/([\w.-]+\.(?:ttf|woff2))", path):
                    return self.send_bytes((WEB / "fonts" / m[1]).read_bytes(), "font/ttf",
                                           extra={"Cache-Control": "max-age=31536000"})
                if path == "/api/config":
                    return self.send_json(dict(app.config(), can_setup=app.setup_enabled))
                if path == "/api/scan":
                    return self.send_json(app.scanner.status())
                if path == "/api/fs":
                    if not app.setup_enabled:
                        return self.send_json({"error": "Choosing folders in the browser is turned off."}, 403)
                    try:
                        return self.send_json(list_dir(parse_qs(url.query).get("path", [""])[0]))
                    except ValueError as e:
                        return self.send_json({"error": str(e)}, 400)
                if not app.has_library():
                    return self.send_json({"error": "no library yet"}, 404)
                if path == "/api/library":
                    return self.send_json(app.library())
                if m := re.fullmatch(r"/api/track/(\d+)", path):
                    t = app.track(int(m[1]))
                    return self.send_json(t) if t else self.send_json({"error": "not found"}, 404)
                if m := re.fullmatch(r"/cover/([0-9a-f]{40})\.jpg", path):
                    c = app.cover(m[1])
                    if not c:
                        return self.send_json({"error": "not found"}, 404)
                    return self.send_bytes(c[1], c[0], extra={"Cache-Control": "max-age=31536000, immutable"})
            except (BrokenPipeError, ConnectionResetError):
                return
            self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if not self.trusted() or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send_json({"error": "forbidden"}, 403)
            if not app.setup_enabled:
                return self.send_json({"error": "Choosing folders in the browser is turned off."}, 403)
            body = self.body_json()
            if body is None:
                return self.send_json({"error": "bad request"}, 400)
            try:
                if path == "/api/config":
                    roots = config.normalise(body.get("roots", []))
                    config.save(app.data, roots)
                    return self.send_json({"roots": roots})
                if path == "/api/scan":
                    roots = config.normalise(body["roots"] if "roots" in body else config.load(app.data).get("roots", []))
                    config.save(app.data, roots)
                    if not app.scanner.start(roots):
                        return self.send_json({"error": "A scan is already running."}, 409)
                    return self.send_json(app.scanner.status(), 202)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            self.send_json({"error": "not found"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Browse your music library's tags and covers.")
    ap.add_argument("folders", nargs="*", help="music folders, PATH or LABEL=PATH (default: the ones saved last time)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data", help="where the database and saved folder list live (default ./data)")
    ap.add_argument("--rescan", action="store_true", help="scan again even if nothing changed")
    ap.add_argument("--remote-setup", action="store_true",
                    help="with a --host other than localhost, still allow choosing folders and scanning in the browser")
    args = ap.parse_args()

    data = config.data_dir(args.data)
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    app = App(data, setup_enabled=loopback or args.remote_setup)
    app.loopback_only = loopback

    saved = config.load(data).get("roots", [])
    roots, changed = saved, False
    if args.folders:
        try:
            roots = config.normalise(args.folders)
        except ValueError as e:
            raise SystemExit(f"error: {e}")
        changed = roots != saved
        config.save(data, roots)
    elif saved:
        try:
            roots = config.normalise(saved)
        except ValueError as e:
            print(f"warning: {e}")          # a saved folder is gone; the setup page lets you fix it

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    srv.daemon_threads = True
    url = f"http://{'localhost' if loopback else args.host}:{args.port}"
    if roots and (args.rescan or changed or not app.has_library()):
        print("Scanning " + ", ".join(f"{r['label']} ({r['path']})" for r in roots) + " ...", flush=True)
        app.scanner.start(roots)
    elif not roots:
        print("No music folders yet: choose them in the browser.", flush=True)
    print(f"Music Library on {url}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
