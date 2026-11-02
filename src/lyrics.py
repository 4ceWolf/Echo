"""Parse .lrc lyric files and embedded tags with time-synced lookup."""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class LyricLine:
    time_ms: int
    text: str


class LyricSet:
    def __init__(self, lines: list[LyricLine], is_synced: bool = True):
        self.lines = sorted(lines, key=lambda l: l.time_ms) if is_synced else lines
        self.is_synced = is_synced

    def index_at(self, position_ms: int) -> int:
        """Return index of the active line for a given playback position (-1 if before first)."""
        if not self.is_synced or not self.lines:
            return -1
        result = -1
        left, right = 0, len(self.lines) - 1
        while left <= right:
            mid = (left + right) // 2
            if self.lines[mid].time_ms <= position_ms:
                result = mid
                left = mid + 1
            else:
                right = mid - 1
        return result

    def __bool__(self):
        return len(self.lines) > 0


def find_lrc_for(audio_path: str) -> Optional[Path]:
    p = Path(audio_path)
    # Direct match: song.mp3 -> song.lrc
    candidate = p.with_suffix(".lrc")
    if candidate.exists():
        return candidate
    # Case-insensitive match in the same directory
    try:
        stem_lower = p.stem.lower()
        for f in p.parent.iterdir():
            if f.is_file() and f.suffix.lower() == ".lrc" and f.stem.lower() == stem_lower:
                return f
    except Exception:
        pass
    return None


def parse_lrc_text(text: str) -> LyricSet:
    lines: list[LyricLine] = []
    has_time_tags = False

    for raw_line in text.splitlines():
        tags = list(TIME_TAG_RE.finditer(raw_line))
        if tags:
            has_time_tags = True
            content = TIME_TAG_RE.sub("", raw_line).strip()
            for m in tags:
                minutes = int(m.group(1))
                seconds = int(m.group(2))
                frac = m.group(3) or "0"
                if len(frac) == 2:
                    frac_ms = int(frac) * 10
                elif len(frac) == 1:
                    frac_ms = int(frac) * 100
                else:
                    frac_ms = int(frac.ljust(3, "0")[:3])
                total_ms = minutes * 60_000 + seconds * 1000 + frac_ms
                lines.append(LyricLine(total_ms, content))
        elif not has_time_tags:
            # Maybe plain unsynced text
            cleaned = raw_line.strip()
            if cleaned:
                lines.append(LyricLine(-1, cleaned))

    if has_time_tags:
        return LyricSet([l for l in lines if l.time_ms >= 0], is_synced=True)
    elif lines:
        return LyricSet(lines, is_synced=False)
    return LyricSet([])


def parse_lrc_file(path: Path) -> LyricSet:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return parse_lrc_text(text)
    except Exception:
        return LyricSet([])


def extract_embedded_lyrics(audio_path: str) -> Optional[str]:
    """Extract embedded lyrics text from audio file tags."""
    if MutagenFile is None:
        return None
    try:
        audio = MutagenFile(audio_path)
        if audio is None or not audio.tags:
            return None
        tags = audio.tags

        # ID3 USLT / SYLT frames
        for key in ("USLT", "USLT::eng", "USLT::XXX", "USLT::und", "LYRICS", "UNSYNCEDLYRICS", "\xa9lyr"):
            if key in tags:
                val = tags[key]
                text = getattr(val, "text", val)
                if isinstance(text, list):
                    text = "\n".join(str(t) for t in text)
                if text and str(text).strip():
                    return str(text)

        # Generic search through tag keys
        for key, val in tags.items():
            k_lower = str(key).lower()
            if "uslt" in k_lower or "lyric" in k_lower:
                text = getattr(val, "text", val)
                if isinstance(text, list):
                    text = "\n".join(str(t) for t in text)
                if text and str(text).strip():
                    return str(text)
    except Exception:
        pass
    return None


def load_lyrics_for(audio_path: str) -> LyricSet:
    # 1. Look for .lrc file next to the audio
    lrc = find_lrc_for(audio_path)
    if lrc is not None:
        lyrics = parse_lrc_file(lrc)
        if lyrics:
            return lyrics

    # 2. Check embedded audio tags
    embedded = extract_embedded_lyrics(audio_path)
    if embedded:
        lyrics = parse_lrc_text(embedded)
        if lyrics:
            return lyrics

    return LyricSet([])
