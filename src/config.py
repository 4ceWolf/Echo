"""App-wide configuration and paths."""
import json
import os
from pathlib import Path

APP_ID = "io.echo.Player"
APP_NAME = "Echo"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "echo"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_MUSIC_DIR = str(Path.home() / "Music")

# Bundled logo asset
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_PATH = ASSETS_DIR / "echo-logo.png"

SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".wma", ".aac", ".oga",
}

CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass

    # Intelligent default: if ~/Music is empty but ~/Downloads has music, use ~/Downloads
    initial_root = DEFAULT_MUSIC_DIR
    music_p = Path(DEFAULT_MUSIC_DIR)
    has_audio = False
    if music_p.exists():
        try:
            for f in music_p.iterdir():
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                    has_audio = True
                    break
        except Exception:
            pass

    if not has_audio:
        dl_p = Path.home() / "Downloads"
        if dl_p.exists():
            try:
                for f in dl_p.iterdir():
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                        initial_root = str(dl_p)
                        break
            except Exception:
                pass

    return {
        "library_root": initial_root,
        "shuffle": False,
        "shuffle_state": 0,
        "repeat": "off",  # off, one, all
        "volume": 1.0,
        "last_track": None,
        "dynamic_color": True,
        "glass_theme": True,
        "show_visualizer": True,
        "lyrics_enabled": True,
        "sidebar_visible": True,
    }


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[config] failed to save: {e}")
