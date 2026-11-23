#!/usr/bin/env python3
"""Build the visualizer tables inside music.db from the extracted track rows.

scan.py runs this after scanning. Run it alone to rebuild the derived
tables after editing the `tracks` table - the audio files are not touched.

    python3 derive.py [--db data/music.db]

Derived (everything the UI shows is read from these, nothing is computed in the browser)
    artists, albums          counts, cover, ordered id lists; see "grouping" below
    genres, track_genres, album_genres
    years                    tracks / albums per year
    flags                    metadata-gap definitions + how many tracks each one hits
    stats                    library-wide totals
    tracks.album_id / artist_id / album_pos / flags / search   filled in here
"""
import argparse
import colorsys
import io
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent

# Grouping: an album is a folder (a CD1/CD2 subfolder counts as part of its parent). The artist comes from
#   - the folder name, when most of a library is laid out Artist/Album/tracks
#   - otherwise the tags: album artist, else the track artist ("Various Artists" if they differ)
DISC_DIR = re.compile(r"(?i)^(cd|disc|disk|vol(ume)?)[\s._-]*\d+\b")

# bit position = index; tracks.flags holds the OR of the bits that apply
FLAGS = [
    ("notrack",       "No track number",                            lambda t: not t["track_no"]),
    ("nolyrics",      "No lyrics",                                  lambda t: not t["lyrics"] and not t["lrc_text"]),
    ("unsynced",      "Lyrics not time-synced",                     lambda t: bool(t["lyrics"]) and not t["lyrics_synced"]),
    ("nomb",          "No MusicBrainz id",                          lambda t: not t["mb_track_id"]),
    ("nobpm",         "No BPM",                                     lambda t: not t["bpm"]),
    ("nocomposer",    "No composer",                                lambda t: not t["composer"]),
    ("nocopyright",   "No copyright",                               lambda t: not t["copyright"]),
    ("noalbumartist", "No album artist",                            lambda t: not t["album_artist"]),
    ("smallcover",    "Cover under 500 px",                         lambda t: bool(t["cover_w"]) and min(t["cover_w"], t["cover_h"]) < 500),
    ("dupes",         "Possible duplicate (same artist + title)",   lambda t: t["_dupe"]),
    ("notitle",       "No title",                                   lambda t: not t["title"]),
    ("noartist",      "No artist",                                  lambda t: not t["artist"]),
    ("noalbum",       "No album",                                   lambda t: not t["album"]),
    ("nogenre",       "No genre",                                   lambda t: not t["genre"]),
    ("noyear",        "No year",                                    lambda t: not t["year"]),
    ("nocover",       "No cover art",                               lambda t: not t["cover_hash"]),
]

DERIVED_SCHEMA = """
DROP TABLE IF EXISTS artists;  DROP TABLE IF EXISTS albums;
DROP TABLE IF EXISTS genres;   DROP TABLE IF EXISTS track_genres; DROP TABLE IF EXISTS album_genres;
DROP TABLE IF EXISTS years;    DROP TABLE IF EXISTS flags;        DROP TABLE IF EXISTS stats;
DROP VIEW  IF EXISTS albums;   DROP VIEW IF EXISTS artists;

CREATE TABLE artists (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
    n_albums INTEGER, n_tracks INTEGER, duration REAL,
    covers TEXT,          -- JSON: up to 4 cover hashes for the mosaic
    album_ids TEXT        -- JSON: album ids
);
CREATE TABLE albums (
    id INTEGER PRIMARY KEY, artist_id INTEGER NOT NULL REFERENCES artists(id),
    source TEXT NOT NULL, folder TEXT,   -- folder inside the source; '' = its top level
    title TEXT, album_artist TEXT, year INTEGER,
    n_tracks INTEGER, duration REAL, file_size INTEGER, mtime REAL,
    cover_hash TEXT, color TEXT, color_key REAL,
    copyright TEXT, label TEXT, mb_album_id TEXT,
    n_lyrics INTEGER, n_synced INTEGER,
    genres TEXT,          -- JSON: genre names, most frequent first
    track_ids TEXT        -- JSON: track ids in disc/track order
);
CREATE TABLE genres (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, n_tracks INTEGER, n_albums INTEGER,
    album_ids TEXT);   -- JSON: albums in this genre, ordered by cover colour
CREATE TABLE track_genres (track_id INTEGER NOT NULL, genre_id INTEGER NOT NULL, PRIMARY KEY (track_id, genre_id));
CREATE TABLE album_genres (album_id INTEGER NOT NULL, genre_id INTEGER NOT NULL, n_tracks INTEGER, PRIMARY KEY (album_id, genre_id));
CREATE INDEX track_genres_genre ON track_genres(genre_id);
CREATE TABLE years (year INTEGER PRIMARY KEY, n_tracks INTEGER, n_albums INTEGER,
    album_ids TEXT);   -- JSON: albums from this year, ordered by cover colour
CREATE TABLE flags (bit INTEGER PRIMARY KEY, key TEXT UNIQUE, label TEXT, n_tracks INTEGER);
CREATE TABLE stats (key TEXT PRIMARY KEY, value REAL);
"""

NEEDED = """id, source, folder, filename, file_size, file_mtime, duration, bitrate,
    title, artist, album, album_artist, track_no, disc_no, year, genre, composer, bpm,
    copyright, publisher, lyrics, lyrics_synced, lrc_text, mb_track_id, mb_album_id,
    cover_hash, cover_w, cover_h"""


def natural(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s or "")]


def mode(values):
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else None


def cover_color(jpeg):
    """Dominant colour of a cover: the most prominent palette entry that is neither
    near-white/black nor grey, unless the whole cover is neutral.
    Returns (#rrggbb, sort key). Sort key: neutrals first by lightness, then by hue."""
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((64, 64))
    q = im.quantize(8, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    colors = sorted(q.getcolors(), reverse=True)
    total = sum(n for n, _ in colors)
    best, best_score = None, -1
    for n, i in colors:
        r, g, b = pal[3 * i:3 * i + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        score = (n / total) * (0.15 + s) * max(0.05, 1 - abs(l - 0.5) * 1.6)
        if score > best_score:
            best, best_score = (r, g, b, h, l, s), score
    r, g, b, h, l, s = best
    key = l if s < 0.12 else 10 + h * 360 + l
    return f"#{r:02x}{g:02x}{b:02x}", key


def norm(s):
    return " ".join((s or "").lower().split())


def album_folder(folder):
    """Folder parts of the album a track belongs to: CD1/CD2 subfolders belong to their parent."""
    parts = [p for p in folder.split("/") if p]
    if len(parts) > 1 and DISC_DIR.match(parts[-1]):
        parts = parts[:-1]
    return tuple(parts)


def primary(artist):
    return norm(artist.split(";")[0])


def artist_of(layout, parts, ts, mixed):
    """Who an album belongs to (see the grouping note at the top)."""
    if layout == "folders" and len(parts) == 2:
        return parts[0]
    named = mode(t["album_artist"] for t in ts)
    if named:
        return named
    artists = Counter(primary(t["artist"]) for t in ts if t["artist"])
    if artists:
        top, n = artists.most_common(1)[0]
        if len(artists) > 1 and n < 0.6 * len(ts):
            return "Various Artists"
        return mode(t["artist"] for t in ts if t["artist"] and primary(t["artist"]) == top)
    return parts[0] if parts and not mixed else "Unknown artist"


def derive(db):
    db.row_factory = sqlite3.Row
    db.executescript(DERIVED_SCHEMA)

    # ---- cover colours
    cover_colors = {}
    for h, data in db.execute("SELECT hash, data FROM covers").fetchall():
        cover_colors[h] = cover_color(data)
        db.execute("UPDATE covers SET color=?, color_key=? WHERE hash=?", (*cover_colors[h], h))
    tracks = [dict(r) for r in db.execute(f"SELECT {NEEDED} FROM tracks ORDER BY id")]

    # ---- possible duplicates (only when both artist and title are known)
    seen = defaultdict(list)
    for t in tracks:
        t["_dupe"] = False
        if t["artist"] and t["title"]:
            seen[(t["artist"].lower(), t["title"].lower())].append(t)
    for group in seen.values():
        if len(group) > 1:
            for t in group:
                t["_dupe"] = True

    # ---- group into albums and artists
    sources = [dict(r) for r in db.execute("SELECT label, position FROM sources ORDER BY position")]
    position = {s["label"]: s["position"] for s in sources}
    for t in tracks:
        t["_parts"] = album_folder(t["folder"])
    layout = {}
    for s in sources:
        depths = Counter(len(t["_parts"]) for t in tracks if t["source"] == s["label"])
        layout[s["label"]] = "folders" if depths[2] >= 0.6 * max(1, sum(depths.values())) else "tags"

    # a folder that mixes several albums (a flat dump of files) is split by the album tag
    in_folder = defaultdict(list)
    for t in tracks:
        in_folder[(t["source"], tuple(t["_parts"]))].append(t)
    split = set()
    for (src, parts), ts in in_folder.items():
        if layout[src] == "tags":
            albums = Counter(norm(t["album"]) for t in ts if t["album"])
            if len(albums) > 1 and albums.most_common(1)[0][1] < 0.8 * len(ts):
                split.add((src, parts))
    by_album = defaultdict(list)
    for t in tracks:
        k = (t["source"], tuple(t["_parts"]))
        by_album[k + ((norm(t["album"]),) if k in split else ())].append(t)

    entries = []
    for key, ts in by_album.items():
        src, parts = key[0], key[1]
        entries.append(dict(src=src, parts=parts, ts=ts, split=key in split,
                            artist=artist_of(layout[src], parts, ts, key in split)))
    entries.sort(key=lambda e: (position[e["src"]], e["artist"].lower(), "/".join(e["parts"]).lower(), len(e["ts"])))
    by_artist = defaultdict(list)
    for e in entries:
        by_artist[(e["src"], e["artist"].lower())].append(e)

    track_updates, genre_tracks = [], defaultdict(set)
    genre_albums = defaultdict(set)
    year_albums = defaultdict(set)
    album_genre_rows = []
    artist_id = album_id = 0
    for (source, _), album_entries in by_artist.items():
        artist_id += 1
        artist_name = album_entries[0]["artist"]
        a_tracks, a_albums, a_covers = [], [], []
        for e in album_entries:
            album_id += 1
            folder = "/".join(e["parts"])
            ts = sorted(e["ts"], key=lambda t: (t["disc_no"] or 1, t["track_no"] or 10**4, natural(t["filename"])))
            gcount = Counter(g for t in ts for g in (t["genre"].split("; ") if t["genre"] else []))
            years = [t["year"] for t in ts if t["year"]]
            cover = mode(t["cover_hash"] for t in ts)
            db.execute(
                """INSERT INTO albums (id, artist_id, source, folder, title, album_artist, year, n_tracks, duration,
                       file_size, mtime, cover_hash, color, color_key, copyright, label, mb_album_id,
                       n_lyrics, n_synced, genres, track_ids)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (album_id, artist_id, source, folder,
                 mode(t["album"] for t in ts) or ("Unknown album" if e["split"] else (e["parts"][-1] if e["parts"] else source)),
                 mode(t["album_artist"] for t in ts) or artist_name,
                 min(years) if years else None,
                 len(ts), sum(t["duration"] or 0 for t in ts), sum(t["file_size"] or 0 for t in ts),
                 max(t["file_mtime"] or 0 for t in ts), cover, *cover_colors.get(cover, (None, 999)),
                 mode(t["copyright"] for t in ts), mode(t["publisher"] for t in ts),
                 mode(t["mb_album_id"] for t in ts),
                 sum(1 for t in ts if t["lyrics"]), sum(1 for t in ts if t["lyrics"] and t["lyrics_synced"]),
                 json.dumps([g for g, _ in gcount.most_common()], ensure_ascii=False),
                 json.dumps([t["id"] for t in ts])))
            for pos, t in enumerate(ts, 1):
                flags = 0
                for bit, (_, _, fn) in enumerate(FLAGS):
                    if fn(t):
                        flags |= 1 << bit
                search = "\n".join(str(x) for x in (
                    t["title"], t["artist"], t["album"], t["album_artist"], t["genre"], t["composer"],
                    t["copyright"], t["publisher"], artist_name, t["folder"], t["year"], t["filename"]) if x).lower()
                track_updates.append((album_id, artist_id, pos, flags, search, t["id"]))
                for g in (t["genre"].split("; ") if t["genre"] else []):
                    genre_tracks[g].add(t["id"])
            for g, n in gcount.items():
                genre_albums[g].add(album_id)
                album_genre_rows.append((album_id, g, n))
            for y in set(years):
                year_albums[y].add(album_id)
            a_tracks += ts
            a_albums.append(album_id)
            if cover and cover not in a_covers:
                a_covers.append(cover)
        db.execute("INSERT INTO artists VALUES (?,?,?,?,?,?,?,?)",
                   (artist_id, source, artist_name, len(a_albums), len(a_tracks),
                    sum(t["duration"] or 0 for t in a_tracks),
                    json.dumps(a_covers[:4]), json.dumps(a_albums)))

    db.executemany("UPDATE tracks SET album_id=?, artist_id=?, album_pos=?, flags=?, search=? WHERE id=?", track_updates)

    album_key = {r[0]: r[1] for r in db.execute("SELECT id, color_key FROM albums")}
    by_color = lambda ids: json.dumps(sorted(ids, key=lambda a: (album_key[a], a)))

    # ---- genres
    gid = {}
    for g in sorted(genre_tracks, key=str.lower):
        cur = db.execute("INSERT INTO genres (name, n_tracks, n_albums, album_ids) VALUES (?,?,?,?)",
                         (g, len(genre_tracks[g]), len(genre_albums[g]), by_color(genre_albums[g])))
        gid[g] = cur.lastrowid
        db.executemany("INSERT INTO track_genres VALUES (?,?)", [(t, gid[g]) for t in genre_tracks[g]])
    db.executemany("INSERT INTO album_genres VALUES (?,?,?)", [(a, gid[g], n) for a, g, n in album_genre_rows])

    # ---- years
    ycount = Counter(t["year"] for t in tracks if t["year"])
    db.executemany("INSERT INTO years VALUES (?,?,?,?)",
                   [(y, n, len(year_albums[y]), by_color(year_albums[y])) for y, n in sorted(ycount.items())])

    # ---- flags
    for bit, (key, label, fn) in enumerate(FLAGS):
        db.execute("INSERT INTO flags VALUES (?,?,?,?)", (bit, key, label, sum(1 for t in tracks if fn(t))))

    # ---- sources (one per scanned folder)
    for s in sources:
        ts = [t for t in tracks if t["source"] == s["label"]]
        db.execute("UPDATE sources SET n_tracks=?, n_albums=(SELECT COUNT(*) FROM albums WHERE source=?), seconds=?, bytes=? WHERE label=?",
                   (len(ts), s["label"], sum(t["duration"] or 0 for t in ts), sum(t["file_size"] or 0 for t in ts), s["label"]))

    # ---- stats
    rates = [t["bitrate"] for t in tracks if t["bitrate"]]
    stats = {
        "tracks": len(tracks), "albums": album_id, "artists": artist_id, "genres": len(gid),
        "seconds": sum(t["duration"] or 0 for t in tracks), "bytes": sum(t["file_size"] or 0 for t in tracks),
        "avg_bitrate": sum(rates) / len(rates) if rates else 0,
    }
    if ycount:
        stats.update(year_min=min(ycount), year_max=max(ycount))
    db.executemany("INSERT INTO stats VALUES (?,?)", stats.items())
    db.commit()
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(HERE / "data" / "music.db"))
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    stats = derive(db)
    db.execute("VACUUM")
    print("derived:", {k: round(v) for k, v in stats.items() if "." not in k})


if __name__ == "__main__":
    main()
