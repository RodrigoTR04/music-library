#!/usr/bin/env python3
"""Scan one or more music folders and write every track's metadata into a SQLite database.

    python3 scan.py ~/Music
    python3 scan.py ~/Music Games=/mnt/disk/soundtracks --data ./data

Each folder becomes one library in the UI. Give it a name with LABEL=PATH, otherwise the
folder's own name is used. Formats: mp3, flac, ogg, opus, m4a/mp4, wav, aiff.

Layer 1 - extracted from the files (this module)
    sources   the folders that were scanned
    tracks    one row per audio file: location, technical info, the common tags as columns,
              lyrics, ReplayGain, MusicBrainz ids
    tags_raw  every tag of every track as (track_id, tag, value)
    covers    de-duplicated cover art, stored as resized JPEG thumbnails
Layer 2 - built for the browser UI (derive.py, run automatically at the end)
"""
import argparse
import hashlib
import io
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from PIL import Image

import config
import derive
import tags

VERSION = 2            # bump when the table layout changes; the server asks for a rescan on a mismatch
COVER_MAX = 512
# dot-files and NAS system folders; "...Baby One More Time" is a real folder name, ".git" is not
HIDDEN = re.compile(r"^\.[^.]|^@eaDir$|^#recycle$")
COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")

SCHEMA = f"""
PRAGMA journal_mode = DELETE;
PRAGMA user_version = {VERSION};
CREATE TABLE sources (
    label     TEXT PRIMARY KEY,            -- what the UI calls this folder
    path      TEXT NOT NULL,
    position  INTEGER NOT NULL,            -- order the folders were given in
    n_tracks  INTEGER, n_albums INTEGER, seconds REAL, bytes INTEGER   -- filled in by derive.py
);
CREATE TABLE tracks (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL REFERENCES sources(label),
    rel_path      TEXT NOT NULL UNIQUE,    -- source label + path inside that folder
    folder        TEXT NOT NULL,           -- directory inside the source folder, '' for its top level
    filename      TEXT,
    file_size     INTEGER,
    file_mtime    REAL,
    -- audio stream
    codec         TEXT,
    tag_format    TEXT,
    duration      REAL,
    bitrate       INTEGER,
    bitrate_mode  TEXT,
    sample_rate   INTEGER,
    channels      INTEGER,
    bit_depth     INTEGER,
    encoder       TEXT,
    -- main tags
    title         TEXT,
    artist        TEXT,
    album         TEXT,
    album_artist  TEXT,
    track_no      INTEGER,
    track_total   INTEGER,
    disc_no       INTEGER,
    disc_total    INTEGER,
    year          INTEGER,
    date_raw      TEXT,                    -- the date tag as written
    original_date TEXT,
    genre         TEXT,
    composer      TEXT,
    bpm           INTEGER,
    musical_key   TEXT,
    copyright     TEXT,
    publisher     TEXT,
    -- lyrics
    lyrics        TEXT,
    lyrics_synced INTEGER NOT NULL DEFAULT 0,  -- the lyrics carry timestamps
    has_sylt      INTEGER NOT NULL DEFAULT 0,
    lrc_text      TEXT,                    -- sidecar .lrc file, if any
    -- ReplayGain
    rg_track_gain TEXT, rg_track_peak REAL,
    rg_album_gain TEXT, rg_album_peak REAL,
    -- MusicBrainz
    mb_track_id TEXT, mb_album_id TEXT, mb_artist_id TEXT,
    mb_albumartist_id TEXT, mb_releasegroup_id TEXT, mb_recording_id TEXT,
    -- cover
    cover_hash    TEXT,
    cover_src     TEXT,                    -- embedded | folder
    cover_w       INTEGER,
    cover_h       INTEGER,
    cover_bytes   INTEGER,
    n_covers      INTEGER NOT NULL DEFAULT 0,   -- images embedded in the file
    n_frames      INTEGER,
    -- filled in by derive.py
    album_id      INTEGER,
    artist_id     INTEGER,
    album_pos     INTEGER,                 -- position within the album
    flags         INTEGER NOT NULL DEFAULT 0,  -- metadata-gap bitmask, see flags table
    search        TEXT                     -- lowercase text the UI search matches against
);
CREATE TABLE tags_raw (
    track_id INTEGER NOT NULL REFERENCES tracks(id),
    frame    TEXT NOT NULL,
    value    TEXT
);
CREATE INDEX tags_raw_track ON tags_raw(track_id);
CREATE TABLE covers (
    hash  TEXT PRIMARY KEY,
    mime  TEXT,
    data  BLOB,
    color TEXT,                            -- dominant colour as #rrggbb, set by derive.py
    color_key REAL                         -- sort position along the colour wheel
);
CREATE INDEX tracks_artist ON tracks(artist);
CREATE INDEX tracks_album  ON tracks(album);
CREATE INDEX tracks_genre  ON tracks(genre);
CREATE INDEX tracks_year   ON tracks(year);
CREATE INDEX tracks_album_id ON tracks(album_id, album_pos);
"""


class ScanError(Exception):
    pass


def make_thumb(data):
    """Return (jpeg_bytes, orig_w, orig_h) or None if the image is unreadable."""
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        im = im.convert("RGB")
        im.thumbnail((COVER_MAX, COVER_MAX), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue(), w, h
    except Exception:
        return None


def find_audio(root):
    """Every audio file under root (symlinked folders followed once), sorted for a stable id order."""
    found, seen = [], set()
    for d, dirs, files in os.walk(root, followlinks=True):
        st = os.stat(d)
        if (st.st_dev, st.st_ino) in seen:       # a symlink loop
            dirs[:] = []
            continue
        seen.add((st.st_dev, st.st_ino))
        dirs[:] = sorted(x for x in dirs if not HIDDEN.match(x))
        for f in files:
            if not HIDDEN.match(f) and Path(f).suffix.lower() in tags.AUDIO_EXTS:
                found.append(Path(d) / f)
    found.sort(key=lambda p: str(p).lower())
    return found


def folder_art(directory, cache):
    """cover.jpg / folder.png / ... sitting next to the tracks, as (bytes) or None."""
    if directory not in cache:
        cache[directory] = None
        try:
            names = {p.name.lower(): p for p in directory.iterdir() if p.is_file()}
        except OSError:
            names = {}
        for stem in COVER_NAMES:
            for ext in COVER_EXTS:
                if stem + ext in names:
                    try:
                        cache[directory] = names[stem + ext].read_bytes()
                    except OSError:
                        pass
                    break
            if cache[directory]:
                break
    return cache[directory]


def read_track(path, root, label, covers, art_cache):
    rel = path.relative_to(root)
    st = path.stat()
    row = {
        "source": label, "rel_path": f"{label}/{rel.as_posix()}",
        "folder": rel.parent.as_posix() if rel.parent.as_posix() != "." else "",
        "filename": path.name, "file_size": st.st_size, "file_mtime": st.st_mtime,
    }
    fields, raws, pics = tags.read(path)
    row.update(fields)
    row["n_covers"] = len(pics)

    data, src = None, None
    if pics:
        front = [p for p in pics if p[1]]
        data, src = (front or pics)[0][0], "embedded"
    else:
        data = folder_art(path.parent, art_cache)
        if not data and derive.DISC_DIR.match(path.parent.name) and path.parent != root:
            data = folder_art(path.parent.parent, art_cache)       # art sits above the CD1/CD2 folders
        src = "folder" if data else None
    if data:
        h = hashlib.sha1(data).hexdigest()
        if h not in covers:
            covers[h] = make_thumb(data)
        if covers[h]:
            row.update(cover_hash=h, cover_src=src, cover_bytes=len(data),
                       cover_w=covers[h][1], cover_h=covers[h][2])
    lrc = path.with_suffix(".lrc")
    if lrc.is_file():
        row["lrc_text"] = lrc.read_text(encoding="utf-8", errors="replace")
    return row, raws


def scan(roots, out, progress=None):
    """Scan roots ([{"label", "path"}]) into a new database at `out` and return a summary dict.

    The old database is replaced only when the whole scan succeeds. progress(phase, done, total)
    is called as it goes; phase is 'finding', 'reading' or 'deriving'."""
    progress = progress or (lambda *a: None)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    progress("finding", 0, 0)
    files = []
    for r in roots:
        found = find_audio(r["path"])
        files += [(p, Path(r["path"]), r["label"]) for p in found]
        progress("finding", len(files), 0)
    if not files:
        raise ScanError("No audio files were found in " + ", ".join(r["path"] for r in roots)
                        + ". Supported: " + ", ".join(sorted(e[1:] for e in tags.AUDIO_EXTS)) + ".")

    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO sources (label, path, position) VALUES (?,?,?)",
                       [(r["label"], r["path"], i) for i, r in enumerate(roots)])
        covers, art_cache, errors, t0 = {}, {}, [], time.time()
        for i, (path, root, label) in enumerate(files, 1):
            try:
                row, raws = read_track(path, root, label, covers, art_cache)
            except Exception as e:             # unreadable file: record it, keep going
                errors.append((str(path), str(e) or repr(e)))
                continue
            keys = list(row)
            cur = db.execute(f"INSERT INTO tracks ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                             [row[k] for k in keys])
            db.executemany("INSERT INTO tags_raw VALUES (?,?,?)",
                           [(cur.lastrowid, k, None if v is None else str(v)) for k, v in raws])
            if i % 25 == 0 or i == len(files):
                progress("reading", i, len(files))
        if len(errors) == len(files):
            raise ScanError(f"None of the {len(files)} audio files could be read. First error: {errors[0][1]}")

        db.executemany("INSERT INTO covers (hash, mime, data) VALUES (?,?,?)",
                       [(h, "image/jpeg", v[0]) for h, v in covers.items() if v])
        db.commit()
        progress("deriving", 0, 0)
        stats = derive.derive(db)
        db.execute("VACUUM")
        db.close()
        os.replace(tmp, out)
    except BaseException:
        db.close()
        tmp.unlink(missing_ok=True)
        raise
    return {"tracks": len(files) - len(errors), "covers": len(covers), "errors": errors,
            "seconds": round(time.time() - t0, 1), "albums": stats["albums"], "artists": stats["artists"]}


def main():
    ap = argparse.ArgumentParser(description="Scan music folders into the library database.")
    ap.add_argument("folders", nargs="*", help="PATH or LABEL=PATH; default: the folders saved by the app")
    ap.add_argument("--data", help="where the database and config live (default ./data)")
    args = ap.parse_args()

    data = config.data_dir(args.data)
    try:
        roots = config.normalise(args.folders or config.load(data)["roots"])
    except ValueError as e:
        raise SystemExit(f"error: {e}")
    config.save(data, roots)

    def show(phase, done, total):
        if phase == "reading" and done % 500 == 0:
            print(f"  {done}/{total}")
    print("Scanning " + ", ".join(f"{r['label']} ({r['path']})" for r in roots))
    try:
        res = scan(roots, config.db_path(data), show)
    except ScanError as e:
        raise SystemExit(f"error: {e}")
    print(f"Done in {res['seconds']} s: {res['tracks']} tracks, {res['albums']} albums, "
          f"{res['artists']} artists, {res['covers']} covers -> {config.db_path(data)}")
    for path, e in res["errors"]:
        print("could not read", path, "-", e, file=sys.stderr)


if __name__ == "__main__":
    main()
