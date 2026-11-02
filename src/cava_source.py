"""
Drives the visualizer from `cava` instead of GStreamer's `spectrum` element.

Why: GStreamer's spectrum element divides the spectrum linearly, so with
music (most energy below ~10kHz) the top bands near Nyquist sit at silence
essentially always - that's the "rightmost bars never move" issue. cava
does perceptual/log frequency binning (like every other desktop audio
visualizer) and reads system audio directly via PulseAudio/PipeWire, so
every bar stays active. Falls back silently to the GStreamer spectrum data
(already wired in player.py) if `cava` isn't installed.
"""
import os
import shutil
import subprocess
import tempfile

from gi.repository import GLib


class CavaSource:
    def __init__(self, bars: int = 32, on_data=None):
        self.bars = bars
        self.on_data = on_data
        self.proc = None
        self._config_path = None
        self._watch_id = None

    @staticmethod
    def is_available() -> bool:
        return shutil.which("cava") is not None

    def start(self) -> bool:
        if not self.is_available():
            return False
        self._config_path = self._write_config()
        try:
            self.proc = subprocess.Popen(
                ["cava", "-p", self._config_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            print(f"[cava] failed to start: {e}")
            self.proc = None
            return False

        channel = GLib.IOChannel.unix_new(self.proc.stdout.fileno())
        self._watch_id = GLib.io_add_watch(
            channel, GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_readable
        )
        return True

    def _write_config(self) -> str:
        fd, path = tempfile.mkstemp(prefix="echo_cava_", suffix=".conf")
        config = f"""
[general]
bars = {self.bars}
framerate = 30

[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 100
channels = mono
"""
        with os.fdopen(fd, "w") as f:
            f.write(config)
        return path

    def _on_readable(self, channel, condition):
        if condition & GLib.IOCondition.HUP:
            return False
        try:
            line = self.proc.stdout.readline()
        except Exception:
            return False
        if not line:
            return False
        try:
            values = [int(v) / 100.0 for v in line.strip().split(";") if v]
            if values and self.on_data:
                self.on_data(values)
        except Exception:
            pass
        return True

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        if self._config_path and os.path.exists(self._config_path):
            try:
                os.remove(self._config_path)
            except Exception:
                pass
