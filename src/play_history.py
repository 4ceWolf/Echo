"""Tracks last-played timestamps per track for smart shuffle and the stats panel."""
import json
import time
from pathlib import Path

HISTORY_FILE = Path.home() / ".config" / "echo" / "play_history.json"
HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def record_play(filepath: str):
    data = load()
    data[filepath] = time.time()
    if len(data) > 5000:
        data = dict(sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:5000])
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[play_history] failed to save: {e}")


def clear_history():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump({}, f)
    except Exception as e:
        print(f"[play_history] failed to clear: {e}")


def last_played(filepath: str) -> float:
    """Epoch seconds, or 0 if never played."""
    return load().get(filepath, 0)
