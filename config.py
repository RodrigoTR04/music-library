"""Which folders the library is built from, and where the app keeps its files.

The folder list lives in <data dir>/config.json:
    {"roots": [{"label": "Music", "path": "/home/me/Music"}, ...]}
Each folder is one "library" in the UI; the label is what the app calls it.
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def data_dir(arg=None):
    return Path(arg or os.environ.get("MUSIC_LIBRARY_DATA") or HERE / "data").expanduser().resolve()


def db_path(data):
    return Path(data) / "music.db"


def normalise(entries):
    """[str | {"path", "label"?}] -> validated [{"label", "path"}]. Raises ValueError with a readable message."""
    roots, labels, paths = [], set(), set()
    for e in entries:
        label, path = (e.get("label"), e.get("path")) if isinstance(e, dict) else (None, e)
        if isinstance(path, str) and "=" in path and not Path(path).exists() and not label:
            label, path = path.split("=", 1)          # LABEL=PATH on the command line
        path = Path(str(path or "").strip()).expanduser()
        if not str(path).strip():
            raise ValueError("A folder path is empty.")
        if not path.is_dir():
            raise ValueError(f"{path} is not a folder that exists.")
        path = path.resolve()
        if path in paths:
            continue
        paths.add(path)
        label = (label or "").strip() or path.name or str(path)
        base, n = label, 2
        while label.lower() in labels:                # two folders both called "Music"
            label, n = f"{base} {n}", n + 1
        labels.add(label.lower())
        roots.append({"label": label, "path": str(path)})
    if not roots:
        raise ValueError("Add at least one folder.")
    return roots


def load(data):
    try:
        return json.loads((Path(data) / "config.json").read_text())
    except (OSError, ValueError):
        return {"roots": []}


def save(data, roots):
    Path(data).mkdir(parents=True, exist_ok=True)
    (Path(data) / "config.json").write_text(json.dumps({"roots": roots}, indent=2, ensure_ascii=False) + "\n")
