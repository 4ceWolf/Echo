"""Scan the library for .m3u/.m3u8 playlists. Each playlist's own tracks keep
their individual embedded album art at playback time (handled by window.py's
existing per-track metadata loading) - the cover found here is only used as a
thumbnail for the playlist entry itself in the sidebar."""
import os
from pathlib import Path

from config import SUPPORTED_EXTENSIONS
import art_utils

PLAYLIST_EXTENSIONS = {".m3u", ".m3u8"}


class Playlist:
    def __init__(self, name: str, m3u_path: str, tracks: list, cover_path: str = None):
        self.name = name
        self.m3u_path = m3u_path
        self.tracks = tracks  # list[str] absolute paths, in playlist order
        self.cover_path = cover_path


def _find_cover(dir_path: Path):
    return art_utils.find_folder_cover_file(dir_path)


def parse_m3u(m3u_path: Path) -> list:
    """Parse a .m3u/.m3u8 file into a list of existing, supported track paths.
    Handles both absolute paths and paths relative to the playlist's own folder,
    and skips blank lines / #EXTINF metadata lines / missing files."""
    tracks = []
    base_dir = m3u_path.parent
    try:
        text = m3u_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return tracks

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        if p.exists() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            tracks.append(str(p))
    return tracks


def scan_playlists(root_path: str) -> list:
    """Recursively find every .m3u/.m3u8 under root_path and build Playlist objects."""
    root = Path(root_path)
    playlists = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if Path(fn).suffix.lower() in PLAYLIST_EXTENSIONS:
                m3u_path = Path(dirpath) / fn
                tracks = parse_m3u(m3u_path)
                if not tracks:
                    continue
                cover = _find_cover(m3u_path.parent)
                if cover is None:
                    # fall back to the first track's own folder, since some
                    # playlists live one level up from the album's folder.jpg
                    cover = _find_cover(Path(tracks[0]).parent)
                cover_path = str(cover) if cover else None
                if cover_path is None:
                    # still nothing - build a 2x2 collage from the first
                    # four tracks' own embedded art instead of no cover at all
                    cover_path = art_utils.build_collage_cover(tracks, cache_key=str(m3u_path))
                playlists.append(Playlist(
                    name=m3u_path.stem,
                    m3u_path=str(m3u_path),
                    tracks=tracks,
                    cover_path=cover_path,
                ))
    playlists.sort(key=lambda pl: pl.name.lower())
    return playlists
