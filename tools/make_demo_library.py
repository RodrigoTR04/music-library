#!/usr/bin/env python3
"""Make a small made-up music library to try the app on (needs ffmpeg, mutagen and Pillow).

    python3 tools/make_demo_library.py demo-music
    python3 server.py demo-music/Albums demo-music/Singles demo-music/Soundtracks

The artists, albums and tracks are invented; the audio is a one-second tone. It covers the
layouts the app has to cope with: Artist/Album folders, a flat folder of mixed singles,
Album-only folders with CD1/CD2 subfolders and folder.jpg art, and every supported format.
"""
import base64
import colorsys
import io
import random
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image, ImageDraw

CODECS = {  # extension -> ffmpeg encoder args
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    ".flac": ["-c:a", "flac"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "4"],
    ".opus": ["-c:a", "libopus", "-b:a", "96k"],
    ".m4a": ["-c:a", "aac", "-b:a", "160k"],
}

# (artist, album, year, genre, ext, [track titles], cover hue)
ALBUMS = [
    ("The Paper Lanterns", "Slow Harbour", 1979, "Folk; Rock", ".mp3", ["Salt Lines", "Low Tide Letter", "Gull Song", "Rope and Rain", "Harbour Lights"], 0.08),
    ("The Paper Lanterns", "Second Winter", 1983, "Rock", ".mp3", ["Frost Work", "Kerosene", "The Long Platform", "Ash Hours"], 0.58),
    ("Marlowe Vance", "Glass Orchard", 1994, "Alternative", ".flac", ["Pear Season", "Glass Orchard", "Wire Fence", "Night Bees", "Ladder", "Quiet Machine"], 0.33),
    ("Marlowe Vance", "Open Windows", 1999, "Alternative; Pop", ".flac", ["Open Windows", "Static Summer", "Cousins", "The Good Chair"], 0.92),
    ("Marlowe Vance", "Unplugged in a Barn", 2003, "Acoustic", ".flac", ["Pear Season (barn)", "Cousins (barn)", "Ladder (barn)"], 0.12),
    ("Okonkwo & Reyes", "Rooftop Atlas", 2008, "Jazz", ".m4a", ["Maps for Pigeons", "Rooftop Atlas", "Seventh Floor Blues", "Antenna", "Afterglow Waltz"], 0.50),
    ("Night Ferry", "Crossing", 2014, "Electronic; Ambient", ".opus", ["Departure", "Mid-Channel", "Fog Signal", "Arrival"], 0.66),
    ("Night Ferry", "Crossing (Remixes)", 2015, "Electronic", ".opus", ["Mid-Channel (Lantern Mix)", "Fog Signal (Dub)"], 0.75),
    ("Imogen Fell", "Small Hours", 2019, "Singer-Songwriter", ".ogg", ["Kettle", "Small Hours", "Borrowed Coat", "Landlines", "Daybreak Shift"], 0.00),
    ("Imogen Fell", "Small Hours (Live at the Annex)", 2021, "Singer-Songwriter", ".ogg", ["Kettle (live)", "Borrowed Coat (live)"], 0.05),
]
SINGLES = [  # one flat folder, mixed albums, FLAC
    ("Delta Kite", "Paper Boats", 2011, "Pop", "Paper Boats"),
    ("Delta Kite", "Paper Boats", 2011, "Pop", "Paper Boats (Instrumental)"),
    ("Mina Ostrow", "Cold Frames", 2017, "Indie", "Cold Frames"),
    ("Mina Ostrow", "Cold Frames", 2017, "Indie", "Slow Reel"),
    ("Delta Kite", "Weathervane EP", 2013, "Pop", "Weathervane"),
    ("Delta Kite", "Weathervane EP", 2013, "Pop", "Tin Roof"),
    ("Mina Ostrow", "Cold Frames", 2017, "Indie", "Gray Pilot"),
    ("Delta Kite", "Weathervane EP", 2013, "Pop", "Sundial"),
]
SOUNDTRACKS = [  # Album/ (CD1, CD2) and folder art, no embedded pictures
    ("Tidal Keep (Original Soundtrack)", 2016, "Soundtrack", [1, 2], 0.55),
    ("Lumen Garden (Original Soundtrack)", 2021, "Soundtrack; Ambient", [1], 0.28),
]
LYRICS = "[00:00.50]Lantern in the window\n[00:03.00]Somebody left it on\n"


ADJ = "Velvet Copper Paper Silent Amber Hollow Neon Salted Wandering Quiet Crimson Pale Brass Drifting Winter Marble Electric Little Glass Broken Golden Distant Wild Slow Iron Fading".split()
NOUN = "Lanterns Harbour Orchard Atlas Meridian Foxes Engines Gardens Signals Pilots Ferries Kites Anchors Rivers Mirrors Coats Stations Choir Cartographers Sparrows Lighthouse Tides Embers".split()
FIRST = "Imogen Marlowe Tobias Anselm Delphine Rosalind Caspian Odette Lucan Marguerite Idris Philippa Soren Linnea Tamsin Evander".split()
LAST = "Fell Vance Okafor Lindqvist Marsh Calloway Reyes Thorne Vasquez Hartley Ibsen Quill Navarro Pike Sorrel Whitlock".split()
WORDS = "Salt Rain Ladder Kettle Static Harbour Pear Wire Cousin Antenna Daybreak Landline Borrowed Coat Frost Platform Ash Gull Rope Fog Signal Arrival Departure Window Summer Machine Bees Fence Atlas Waltz Afterglow Pigeon Roof Blue Mile Lantern Orchard Tide Letter Light Chair Season Cold Reel Pilot Sundial Weather Tin Boat".split()
GENRES = ["Rock", "Alternative", "Folk", "Pop", "Jazz", "Electronic", "Ambient", "Indie", "Singer-Songwriter", "Soul", "Blues", "Classical", "Hip-Hop", "Punk", "Country", "Metal"]


def more_albums(n=64):
    """A deterministic crowd of invented albums so the wall, the year chart and the lists have something to show."""
    rnd = random.Random(7)
    artists = []
    while len(artists) < 26:
        name = f"The {rnd.choice(ADJ)} {rnd.choice(NOUN)}" if rnd.random() < .45 else f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        if name not in artists and name not in {a[0] for a in ALBUMS}:
            artists.append(name)
    out, used = [], set()
    for i in range(n):
        artist = artists[i % len(artists)]
        album = " ".join(rnd.sample(WORDS, rnd.choice([1, 2, 2, 3])))
        if (artist, album) in used:
            continue
        used.add((artist, album))
        year = min(2024, int(rnd.triangular(1966, 2024, 2008)))
        genre = "; ".join(rnd.sample(GENRES, rnd.choice([1, 1, 2])))
        titles = []
        while len(titles) < rnd.randint(4, 9):
            t = " ".join(rnd.sample(WORDS, rnd.choice([1, 2, 2, 3])))
            if t not in titles:
                titles.append(t)
        out.append((artist, album, year, genre, rnd.choice([".mp3", ".mp3", ".flac", ".flac", ".m4a", ".ogg", ".opus"]), titles, rnd.random()))
    return out


def hash_seed(s):
    return zlib.crc32(s.encode())


def cover(hue, size=600, mark="", seed=0):
    rnd = random.Random(seed)
    im = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(im)
    for y in range(size):
        r, g, b = colorsys.hls_to_rgb(hue % 1, 0.25 + 0.4 * y / size, 0.55)
        d.line([(0, y), (size, y)], fill=(int(r * 255), int(g * 255), int(b * 255)))
    for _ in range(5):
        x, y, s = rnd.randrange(size), rnd.randrange(size), rnd.randrange(40, 260)
        r, g, b = colorsys.hls_to_rgb((hue + rnd.random() * 0.1) % 1, 0.8, 0.5)
        d.ellipse([x - s, y - s, x + s, y + s], outline=(int(r * 255), int(g * 255), int(b * 255)), width=6)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def encode(path, ext, tags, hue=None, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    freq = 220 + zlib.crc32(tags.get("title", "").encode()) % 400
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1.2", *CODECS[ext]]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [str(path)], check=True)
    if hue is None:
        return
    img = cover(hue, seed=seed)
    if ext == ".mp3":
        t = ID3(path)
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front", data=img))
        t.save(v2_version=3)
    elif ext == ".flac":
        f = FLAC(path)
        p = Picture()
        p.data, p.type, p.mime, p.width, p.height = img, 3, "image/jpeg", 600, 600
        f.add_picture(p)
        f.save()
    elif ext in (".ogg", ".opus"):
        f = (OggVorbis if ext == ".ogg" else OggOpus)(path)
        p = Picture()
        p.data, p.type, p.mime, p.width, p.height = img, 3, "image/jpeg", 600, 600
        f["metadata_block_picture"] = [base64.b64encode(p.write()).decode()]
        f.save()
    elif ext == ".m4a":
        f = MP4(path)
        f["covr"] = [MP4Cover(img, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is needed to make the demo audio.")
    out = Path(sys.argv[1])
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} already has files in it; pick an empty or new folder.")

    for artist, album, year, genre, ext, titles, hue in ALBUMS + more_albums():
        for i, title in enumerate(titles, 1):
            tags = dict(title=title, artist=artist, album=album, album_artist=artist, track=f"{i}/{len(titles)}",
                        date=str(year), genre=genre)
            if i == 1:
                tags["lyrics"] = LYRICS
            if ext == ".mp3" and i == 2:
                tags["composer"] = artist
            encode(out / "Albums" / artist / album / f"{i:02d} {title}{ext}", ext, tags, hue, seed=hash_seed(album))
    for k, (artist, album, year, genre, title) in enumerate(SINGLES):
        encode(out / "Singles" / f"{artist} - {title}.flac", ".flac",
               dict(title=title, artist=artist, album=album, date=str(year), genre=genre),
               {"Paper Boats": 0.15, "Cold Frames": 0.6, "Weathervane EP": 0.4}[album], seed=100)
    for album, year, genre, discs, hue in SOUNDTRACKS:
        folder = out / "Soundtracks" / album
        for disc in discs:
            for i in range(1, 4):
                title = f"Theme {disc}.{i}"
                sub = folder / f"CD{disc}" if len(discs) > 1 else folder
                encode(sub / f"{i:02d} {title}.mp3", ".mp3",
                       dict(title=title, artist="Various Composers", album=album, track=str(i), disc=f"{disc}/{len(discs)}",
                            date=str(year), genre=genre))
        (folder / "folder.jpg").write_bytes(cover(hue, seed=200))
    # one file with no tags at all
    encode(out / "Singles" / "unlabelled recording.mp3", ".mp3", {})
    print(f"Made {sum(1 for _ in out.rglob('*') if _.suffix in CODECS)} tracks in {out}")


if __name__ == "__main__":
    main()
