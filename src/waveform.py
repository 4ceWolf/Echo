"""Extracts peak-amplitude waveform data from an audio file for the
Amberol-style "spiky" progress bar, computed once per track and cached to
disk. Decoding happens on a background thread since it has to read the
whole file; the result is delivered back via GLib.idle_add so callers never
have to worry about doing GTK calls off the main thread.
"""
import hashlib
import json
import struct
import threading
from pathlib import Path

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")  # required so PyGObject knows appsink's
                                       # actual AppSink methods (try_pull_sample
                                       # etc.) exist - without this import, the
                                       # object PyGObject hands back is wrapped
                                       # as a plain Gst.Element and is missing
                                       # every AppSink-specific method.
from gi.repository import Gst, GstApp, GLib

Gst.init(None)

WAVEFORM_CACHE_DIR = Path.home() / ".cache" / "echo" / "waveforms"
WAVEFORM_CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_BUCKETS = 120


def _cache_path(filepath: str, buckets: int) -> Path:
    h = hashlib.sha1(f"{filepath}:{buckets}".encode("utf-8")).hexdigest()
    return WAVEFORM_CACHE_DIR / f"{h}.json"


def get_cached_peaks(filepath: str, buckets: int = DEFAULT_BUCKETS):
    cache = _cache_path(filepath, buckets)
    if cache.exists():
        try:
            with open(cache, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def extract_peaks_async(filepath: str, on_done, buckets: int = DEFAULT_BUCKETS):
    """on_done(peaks: list[float] | None) is called on the GTK main thread
    once extraction finishes (or immediately, from cache)."""
    cached = get_cached_peaks(filepath, buckets)
    if cached is not None:
        on_done(cached)
        return

    def worker():
        peaks = _extract_peaks_blocking(filepath, buckets)
        GLib.idle_add(on_done, peaks)

    threading.Thread(target=worker, daemon=True).start()


def _extract_peaks_blocking(filepath: str, buckets: int):
    """Decode the whole file via GStreamer to raw mono S16LE and compute a
    peak (max abs sample) per bucket. Runs synchronously - call only from a
    background thread."""
    try:
        uri = Gst.filename_to_uri(filepath)
    except Exception as e:
        print(f"[waveform] bad path {filepath}: {e}")
        return None

    pipeline_desc = (
        f'uridecodebin uri="{uri}" ! audioconvert ! audioresample ! '
        f'audio/x-raw,format=S16LE,channels=1,rate=11025 ! appsink name=sink sync=false'
    )
    try:
        pipeline = Gst.parse_launch(pipeline_desc)
    except GLib.Error as e:
        print(f"[waveform] failed to build pipeline: {e}")
        return None

    appsink = pipeline.get_by_name("sink")
    appsink.set_property("emit-signals", False)
    appsink.set_property("max-buffers", 0)

    pipeline.set_state(Gst.State.PLAYING)

    samples = bytearray()
    bus = pipeline.get_bus()
    done = False
    while not done:
        sample = appsink.try_pull_sample(200_000_000)  # 200ms timeout per pull
        if sample is not None:
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                samples.extend(mapinfo.data)
                buf.unmap(mapinfo)
            continue
        # no sample ready - check whether the stream has actually ended
        msg = bus.pop_filtered(Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if msg is not None:
            done = True

    pipeline.set_state(Gst.State.NULL)

    if not samples:
        return None

    n_samples = len(samples) // 2  # S16LE = 2 bytes/sample
    if n_samples == 0:
        return None

    values = struct.unpack(f"<{n_samples}h", bytes(samples[: n_samples * 2]))
    chunk_size = max(1, n_samples // buckets)
    peaks = []
    for i in range(0, n_samples, chunk_size):
        chunk = values[i:i + chunk_size]
        if not chunk:
            continue
        peak = max(abs(v) for v in chunk) / 32768.0
        peaks.append(peak)
        if len(peaks) >= buckets:
            break

    if len(peaks) < buckets:
        peaks.extend([0.0] * (buckets - len(peaks)))
    peaks = peaks[:buckets]

    try:
        with open(_cache_path(filepath, buckets), "w") as f:
            json.dump(peaks, f)
    except Exception as e:
        print(f"[waveform] failed to cache peaks: {e}")

    return peaks
