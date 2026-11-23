"""Read one audio file into a flat dict of normalised fields.

Every format keeps its tags differently (ID3 frames, Vorbis comments, MP4 atoms); this module
maps them all onto the same fields so the rest of the app never has to care.

    read(path) -> (fields, raw_rows, pictures)
        fields    column name -> value, ready for the `tracks` table
        raw_rows  [(tag name as written in the file, value)] for every tag found
        pictures  [(image bytes, is_front_cover)]
"""
import base64
import re
from pathlib import Path

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import ID3
from mutagen.mp3 import BitrateMode
from mutagen.mp4 import MP4Tags
from mutagen._vorbis import VComment

# extension -> shown nowhere, but decides which files the scanner picks up
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".mp4", ".wav", ".aif", ".aiff"}

BITRATE_MODES = {BitrateMode.CBR: "CBR", BitrateMode.VBR: "VBR", BitrateMode.ABR: "ABR"}


# ---------------------------------------------------------------- small helpers

def clean(values):
    """Join a list of values into one display string, dropping blanks."""
    out = [str(v).strip() for v in values if v is not None and str(v).strip()]
    return "; ".join(out) or None


def text(frame):
    if frame is None:
        return None
    vals = getattr(frame, "text", None)
    return clean(vals) if vals is not None else clean([frame])


def to_int(s):
    m = re.match(r"\s*(\d+)", str(s) if s is not None else "")
    return int(m.group(1)) if m else None


def pair(s):
    """'3/12' -> (3, 12); '3' -> (3, None)."""
    if not s:
        return None, None
    a, _, b = str(s).partition("/")
    return to_int(a), to_int(b)


def to_float(s):
    try:
        return float(re.match(r"\s*(-?[\d.]+)", str(s) if s is not None else "").group(1))
    except (AttributeError, ValueError):
        return None


def synced(lyrics):
    """Does a lyrics blob carry [mm:ss] timestamps (LRC style)?"""
    return int(bool(lyrics and re.search(r"^\s*\[\d+:\d+", lyrics, re.M)))


def first_year(s):
    m = re.search(r"\b(\d{4})\b", s or "")
    return int(m.group(1)) if m else to_int(s)


def decode(b):
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b)


# ---------------------------------------------------------------- ID3 (mp3, wav, aiff)

def from_id3(tags):
    f, raw, pics = {}, [], []
    f["tag_format"] = "ID3v2.%d.%d" % tags.version[1:]
    f["n_frames"] = len(tags)

    def txxx(*names):
        want = {n.lower() for n in names}
        for key, fr in tags.items():
            if key.startswith("TXXX:") and key[5:].lower() in want:
                return text(fr)
        return None

    g = lambda k: text(tags.get(k))
    f["title"], f["artist"], f["album"], f["album_artist"] = g("TIT2"), g("TPE1"), g("TALB"), g("TPE2")
    f["track_no"], f["track_total"] = pair(g("TRCK"))
    f["disc_no"], f["disc_total"] = pair(g("TPOS"))
    f["date_raw"] = g("TDRC") or g("TYER")
    f["original_date"] = g("TDOR") or g("TORY")
    f["genre"], f["composer"] = g("TCON"), g("TCOM")
    f["bpm"], f["musical_key"] = to_int(g("TBPM")), g("TKEY")
    f["copyright"], f["publisher"] = g("TCOP"), g("TPUB")

    for fr in tags.getall("USLT"):
        if fr.text and fr.text.strip():
            f["lyrics"] = fr.text
            f["lyrics_synced"] = synced(fr.text)
            break
    if tags.getall("SYLT"):
        f["has_sylt"], f["lyrics_synced"] = 1, 1

    f["rg_track_gain"] = txxx("REPLAYGAIN_TRACK_GAIN")
    f["rg_track_peak"] = to_float(txxx("REPLAYGAIN_TRACK_PEAK"))
    f["rg_album_gain"] = txxx("REPLAYGAIN_ALBUM_GAIN")
    f["rg_album_peak"] = to_float(txxx("REPLAYGAIN_ALBUM_PEAK"))
    f["mb_track_id"] = txxx("MusicBrainz Track Id", "MusicBrainz Release Track Id")
    f["mb_album_id"] = txxx("MusicBrainz Album Id")
    f["mb_artist_id"] = txxx("MusicBrainz Artist Id")
    f["mb_albumartist_id"] = txxx("MusicBrainz Album Artist Id")
    f["mb_releasegroup_id"] = txxx("MusicBrainz Release Group Id")
    for fr in tags.getall("UFID"):
        if fr.owner == "http://musicbrainz.org":
            f["mb_recording_id"] = fr.data.rstrip(bytes(1)).decode("ascii", "replace")

    for fr in tags.getall("APIC"):
        pics.append((fr.data, fr.type == 3))

    for key, fr in tags.items():
        frame = key.split(":")[0]
        if frame == "APIC":
            val = f"[{fr.mime}, type {fr.type}, {len(fr.data)} bytes]"
        elif frame == "USLT":
            val = f"[{fr.lang}, {len(fr.text)} chars]"
        elif frame == "SYLT":
            val = f"[{fr.lang}, {len(fr.text)} synced lines]"
        elif frame == "UFID":
            val = f"{fr.owner}: {fr.data.rstrip(bytes(1)).decode('utf-8', 'replace')}"
        elif frame == "TXXX":
            key, val = f"TXXX:{fr.desc}", "; ".join(str(v) for v in fr.text)
        else:
            val = text(fr)
        raw.append((key, val))
    return f, raw, pics


# ---------------------------------------------------------------- Vorbis comments (flac, ogg, opus)

def from_vorbis(audio):
    tags = audio.tags
    f, raw, pics = {"tag_format": "Vorbis comments"}, [], []
    lower = {}
    for k, v in list(tags):                     # (key, value) pairs, duplicates kept
        lower.setdefault(k.lower(), []).append(v)
        raw.append((k.upper(), v))

    def g(*names):
        for n in names:
            if lower.get(n):
                return clean(lower[n])
        return None

    f["title"], f["artist"], f["album"] = g("title"), g("artist"), g("album")
    f["album_artist"] = g("albumartist", "album artist")
    f["track_no"], f["track_total"] = pair(g("tracknumber", "track"))
    f["track_total"] = f["track_total"] or to_int(g("tracktotal", "totaltracks"))
    f["disc_no"], f["disc_total"] = pair(g("discnumber", "disc"))
    f["disc_total"] = f["disc_total"] or to_int(g("disctotal", "totaldiscs"))
    f["date_raw"] = g("date", "year")
    f["original_date"] = g("originaldate", "originalyear")
    f["genre"], f["composer"] = g("genre"), g("composer")
    f["bpm"], f["musical_key"] = to_int(g("bpm")), g("initialkey", "key")
    f["copyright"], f["publisher"] = g("copyright"), g("label", "organization", "publisher")
    lyrics = g("lyrics", "unsyncedlyrics")
    if lyrics:
        f["lyrics"], f["lyrics_synced"] = lyrics, synced(lyrics)
    f["rg_track_gain"], f["rg_track_peak"] = g("replaygain_track_gain"), to_float(g("replaygain_track_peak"))
    f["rg_album_gain"], f["rg_album_peak"] = g("replaygain_album_gain"), to_float(g("replaygain_album_peak"))
    f["mb_track_id"] = g("musicbrainz_releasetrackid", "musicbrainz_trackid")
    f["mb_album_id"], f["mb_artist_id"] = g("musicbrainz_albumid"), g("musicbrainz_artistid")
    f["mb_albumartist_id"] = g("musicbrainz_albumartistid")
    f["mb_releasegroup_id"] = g("musicbrainz_releasegroupid")
    f["mb_recording_id"] = g("musicbrainz_trackid") if g("musicbrainz_releasetrackid") else None
    f["n_frames"] = len(raw)

    blocks = list(getattr(audio, "pictures", []) or [])          # FLAC
    for b64 in lower.get("metadata_block_picture", []):          # Ogg / Opus
        try:
            blocks.append(Picture(base64.b64decode(b64)))
        except Exception:
            pass
    for p in blocks:
        pics.append((p.data, p.type == 3))
    raw = [(k, f"[picture, {len(v)} chars]" if k == "METADATA_BLOCK_PICTURE" else v) for k, v in raw]
    for p in getattr(audio, "pictures", []) or []:
        raw.append(("PICTURE", f"[{p.mime}, type {p.type}, {len(p.data)} bytes]"))
    return f, raw, pics


# ---------------------------------------------------------------- MP4 atoms (m4a, m4b, mp4)

def from_mp4(tags):
    f, raw, pics = {"tag_format": "MP4 atoms"}, [], []
    free = {}
    for k, v in tags.items():
        if k.startswith("----:"):
            name = k.split(":", 2)[-1]
            free[name.lower()] = clean(decode(x) for x in v)

    def atom(key):
        return clean(decode(x) if isinstance(x, bytes) else x for x in tags.get(key, []))

    def num(key):
        v = tags.get(key)
        return (v[0][0] or None, v[0][1] or None) if v and isinstance(v[0], tuple) else (None, None)

    ff = lambda *names: next((free[n.lower()] for n in names if free.get(n.lower())), None)

    f["title"], f["artist"], f["album"] = atom("\xa9nam"), atom("\xa9ART"), atom("\xa9alb")
    f["album_artist"] = atom("aART")
    f["track_no"], f["track_total"] = num("trkn")
    f["disc_no"], f["disc_total"] = num("disk")
    f["date_raw"] = atom("\xa9day")
    f["original_date"] = ff("originaldate", "originalyear")
    f["genre"] = atom("\xa9gen")
    f["composer"] = atom("\xa9wrt")
    tmpo = tags.get("tmpo")
    f["bpm"] = int(tmpo[0]) if tmpo else None
    f["musical_key"] = ff("initialkey", "key")
    f["copyright"] = atom("cprt")
    f["publisher"] = ff("label", "publisher")
    lyrics = atom("\xa9lyr")
    if lyrics:
        f["lyrics"], f["lyrics_synced"] = lyrics, synced(lyrics)
    f["rg_track_gain"], f["rg_track_peak"] = ff("replaygain_track_gain"), to_float(ff("replaygain_track_peak"))
    f["rg_album_gain"], f["rg_album_peak"] = ff("replaygain_album_gain"), to_float(ff("replaygain_album_peak"))
    f["mb_track_id"] = ff("MusicBrainz Release Track Id", "MusicBrainz Track Id")
    f["mb_album_id"], f["mb_artist_id"] = ff("MusicBrainz Album Id"), ff("MusicBrainz Artist Id")
    f["mb_albumartist_id"] = ff("MusicBrainz Album Artist Id")
    f["mb_releasegroup_id"] = ff("MusicBrainz Release Group Id")
    f["mb_recording_id"] = ff("MusicBrainz Track Id") if ff("MusicBrainz Release Track Id") else None
    f["n_frames"] = len(tags)

    for c in tags.get("covr", []):
        pics.append((bytes(c), True))
    for k, v in tags.items():
        if k == "covr":
            val = f"[{len(v)} cover image(s)]"
        elif k.startswith("----:"):
            k, val = k.split(":", 2)[-1], clean(decode(x) for x in v)
        elif k in ("trkn", "disk"):
            val = "/".join(str(n) for n in v[0] if n)
        else:
            val = clean(decode(x) if isinstance(x, bytes) else x for x in v)
        raw.append((k.replace("\xa9", "(c)"), val))
    return f, raw, pics


# ---------------------------------------------------------------- the entry point

def codec_name(audio):
    cls = type(audio).__name__
    if cls == "MP4":
        c = (getattr(audio.info, "codec", "") or "").lower()
        return "ALAC" if c == "alac" else "AAC" if c.startswith("mp4a") else (c.upper() or "MP4")
    return {"MP3": "MP3", "FLAC": "FLAC", "OggVorbis": "Vorbis", "OggOpus": "Opus", "OggFlac": "FLAC",
            "WAVE": "WAV", "AIFF": "AIFF"}.get(cls, cls)


def read(path):
    path = Path(path)
    audio = mutagen.File(path)
    if audio is None or audio.info is None:
        raise ValueError("not a readable audio file")
    info = audio.info
    duration = float(getattr(info, "length", 0) or 0)
    bitrate = getattr(info, "bitrate", None) or (path.stat().st_size * 8 / duration if duration else None)
    fields = dict(
        codec=codec_name(audio), duration=duration, bitrate=int(bitrate) if bitrate else None,
        bitrate_mode=BITRATE_MODES.get(getattr(info, "bitrate_mode", None)),
        sample_rate=getattr(info, "sample_rate", None), channels=getattr(info, "channels", None),
        bit_depth=getattr(info, "bits_per_sample", None) or None,
        encoder=getattr(info, "encoder_info", None) or None,
    )
    tags = audio.tags
    extra, raw, pics = {}, [], []
    if isinstance(tags, ID3):
        extra, raw, pics = from_id3(tags)
    elif isinstance(tags, MP4Tags):
        extra, raw, pics = from_mp4(tags)
    elif isinstance(tags, VComment):
        extra, raw, pics = from_vorbis(audio)
    fields.update({k: v for k, v in extra.items() if v is not None})
    fields["year"] = first_year(fields.get("date_raw"))
    return fields, raw, pics
