"""GStreamer playback engine: playback control, DSP (speed/pitch, 8D
auto-pan, concert hall reverb + crossfeed, mono downmix), gapless
playback, and spectrum analysis for the visualizer."""
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject, GLib
import random
import math
import time

import play_history

Gst.init(None)

REPEAT_OFF = "off"
REPEAT_ONE = "one"
REPEAT_ALL = "all"


class Player(GObject.Object):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "position-changed": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),  # pos_ms, dur_ms
        "spectrum": (GObject.SignalFlags.RUN_FIRST, None, (object,)),  # list[float] magnitudes
        "track-finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "eos": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "track-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # filepath
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),  # shuffle/repeat changed
        "queue-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),  # queue contents modified in place
        "dsp-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),  # speed/pitch/8D/mono/balance changed
    }

    def __init__(self):
        super().__init__()
        self.pipeline = Gst.ElementFactory.make("playbin", "player")

        self.spectrum = Gst.ElementFactory.make("spectrum", "spectrum")
        self.spectrum.set_property("bands", 32)
        self.spectrum.set_property("threshold", -70)
        self.spectrum.set_property("post-messages", True)
        self.spectrum.set_property("interval", 50_000_000)  # 50ms in ns

        audio_sink = Gst.ElementFactory.make("bin", "audio_sink_bin")
        convert1 = Gst.ElementFactory.make("audioconvert", "convert1")
        convert2 = Gst.ElementFactory.make("audioconvert", "convert2")
        real_sink = Gst.ElementFactory.make("autoaudiosink", "real_sink")

        # DSP elements. Both come from optional GStreamer plugin sets
        # (pitch needs gst-plugins-bad's soundtouch, audiopanorama needs
        # gst-plugins-good) so we degrade gracefully to plain passthrough
        # if either is missing rather than failing to build the pipeline.
        self.pitch_elem = Gst.ElementFactory.make("pitch", "pitch")
        self.panorama_elem = Gst.ElementFactory.make("audiopanorama", "panorama")
        # A "volume" element whose gain gets modulated in sync with the 8D
        # pan sweep - quieter at the extremes, full at center. Plain L/R
        # panning alone reads as "audio sliding on a flat line" (fair
        # complaint); this depth cue is what makes it read as the sound
        # actually moving away from you and back, not just sideways.
        self.depth_volume = Gst.ElementFactory.make("volume", "depth_volume")
        # Low-pass filter, modulated in sync with the pan: real "moving
        # away / behind you" sound loses high-frequency content (how a
        # muffled voice through a wall or someone facing away sounds) - a
        # far stronger perceptual cue than volume alone, which is why the
        # pan+volume version still just read as flat left-right panning.
        self.depth_filter = Gst.ElementFactory.make("audiocheblimit", "depth_filter")
        if self.depth_filter is not None:
            self.depth_filter.set_property("mode", 0)   # 0 = low-pass
            self.depth_filter.set_property("poles", 4)
            self.depth_filter.set_property("cutoff", 20000.0)  # wide open = no filtering
        self.monocaps = Gst.ElementFactory.make("capsfilter", "monocaps")
        self.monocaps.set_property("caps", Gst.Caps.from_string("audio/x-raw"))

        # "Concert Hall" mode - static room ambience + headphone crossfeed,
        # deliberately NOT the 8D sweep above. No panning motion; the goal
        # is "you're sitting in a room with real speakers" rather than
        # "sound orbiting your head".
        #
        # freeverb (gst-plugins-bad) adds the room itself: reflections and
        # a decay tail sized/damped like a hall rather than a small room.
        # bs2b (gst-plugins-bad, libbs2b) is a crossfeed filter - it blends
        # a bit of each channel into the other the way your ears would
        # naturally pick up both speakers in a real room, which is most of
        # what makes headphone stereo feel "in your skull" rather than "in
        # front of you". Both are optional the same way pitch/panorama are:
        # if the plugin isn't installed, concert hall mode just no-ops.
        self.reverb_elem = Gst.ElementFactory.make("freeverb", "reverb")
        if self.reverb_elem is not None:
            self.reverb_elem.set_property("room-size", 0.97)  # near-max: a real hall, not a room
            self.reverb_elem.set_property("damping", 0.25)    # let the tail ring instead of dying fast
            self.reverb_elem.set_property("width", 1.0)       # full stereo spread of the reflections
            self.reverb_elem.set_property("level", 0.0)       # dry/wet - 0 = off until enabled
        self.bs2b_elem = Gst.ElementFactory.make("bs2b", "bs2b")
        if self.bs2b_elem is not None:
            self.bs2b_elem.set_property("fcut", 700)  # bs2b "cmoy" preset cutoff
            self.bs2b_elem.set_property("feed", 10)     # 10 is min feed (off)

        if self.panorama_elem is not None:
            # psychoacoustic (1) gives a wider, more natural stereo image
            # during the sweep than simple linear panning (0).
            self.panorama_elem.set_property("method", 1)

        chain = [convert1]
        if self.pitch_elem is not None:
            chain.append(self.pitch_elem)
        if self.panorama_elem is not None:
            chain.append(self.panorama_elem)
        if self.depth_volume is not None:
            chain.append(self.depth_volume)
        if self.depth_filter is not None:
            chain.append(self.depth_filter)
        if self.reverb_elem is not None:
            chain.append(self.reverb_elem)
        if self.bs2b_elem is not None:
            chain.append(self.bs2b_elem)
        chain += [self.monocaps, convert2, self.spectrum, real_sink]

        for el in chain:
            audio_sink.add(el)
        for a, b in zip(chain, chain[1:]):
            a.link(b)

        pad = convert1.get_static_pad("sink")
        ghost_pad = Gst.GhostPad.new("sink", pad)
        ghost_pad.set_active(True)
        audio_sink.add_pad(ghost_pad)

        self.pipeline.set_property("audio-sink", audio_sink)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # "about-to-finish" is playbin's gapless hook - fired a little
        # before the current stream ends, giving us a chance to point it at
        # the next URI directly so the transition happens with no re-preroll
        # gap. Connected unconditionally; the handler itself checks
        # self.gapless_enabled and no-ops when it's off.
        self.pipeline.connect("about-to-finish", self._on_about_to_finish)

        self.current_uri = None
        self.current_path = None
        self.duration_ms = 0
        self._position_timeout_id = None

        self.shuffle = False
        self.smart_shuffle = False
        self.repeat = REPEAT_OFF
        self.queue: list[str] = []
        self.history: list[int] = []
        self.queue_index = -1

        self._desired_playing = False

        # DSP state
        self.gapless_enabled = False
        self._gapless_pending_path = None
        self.speed = 1.0
        self.mono_downmix = False
        self.balance = 0.0  # -1.0 (left) .. 1.0 (right)
        self.eightd_enabled = False
        self._eightd_timeout_id = None
        self._eightd_start_time = 0.0
        self.concert_hall_enabled = False

    # --- Playback control ---
    def load_and_play(self, filepath: str):
        self._load(filepath, autoplay=True)

    def load_paused(self, filepath: str):
        """Load a track without starting playback (used for restoring last session)."""
        self._load(filepath, autoplay=False)

    def _load(self, filepath: str, autoplay: bool):
        self.pipeline.set_state(Gst.State.NULL)
        self.current_path = filepath
        self.current_uri = Gst.filename_to_uri(filepath)
        self.pipeline.set_property("uri", self.current_uri)
        self.duration_ms = 0
        self._desired_playing = autoplay
        self.pipeline.set_state(Gst.State.PLAYING if autoplay else Gst.State.PAUSED)
        self._ensure_position_timer()
        if autoplay:
            play_history.record_play(filepath)
        self.emit("track-changed", filepath)
        self.emit("state-changed", "playing" if autoplay else "paused")

    def play(self):
        if self.current_uri:
            self._desired_playing = True
            self.pipeline.set_state(Gst.State.PLAYING)
            self._ensure_position_timer()
            self.emit("state-changed", "playing")

    def pause(self):
        self._desired_playing = False
        self.pipeline.set_state(Gst.State.PAUSED)
        self.emit("state-changed", "paused")

    def toggle_play_pause(self):
        if self._desired_playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        self._desired_playing = False
        self.pipeline.set_state(Gst.State.NULL)
        self.current_uri = None
        self.current_path = None
        self._gapless_pending_path = None
        self.emit("state-changed", "stopped")

    def seek(self, position_ms: int):
        self.pipeline.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            position_ms * Gst.MSECOND
        )

    def set_volume(self, volume: float):
        self.pipeline.set_property("volume", max(0.0, min(1.0, volume)))

    def get_position_ms(self) -> int:
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if ok:
            return pos // Gst.MSECOND
        return 0

    def get_duration_ms(self) -> int:
        ok, dur = self.pipeline.query_duration(Gst.Format.TIME)
        if ok:
            self.duration_ms = dur // Gst.MSECOND
            return self.duration_ms
        return self.duration_ms

    def is_playing(self) -> bool:
        return self._desired_playing

    # --- DSP: speed/pitch ---
    def dsp_available(self) -> dict:
        """Which DSP features actually have a working GStreamer element
        behind them on this system (missing an optional plugin degrades
        gracefully rather than crashing)."""
        return {
            "speed": self.pitch_elem is not None,
            "balance_8d": self.panorama_elem is not None,
            "concert_hall": self.reverb_elem is not None or self.bs2b_elem is not None,
        }

    def set_speed(self, tempo: float):
        """0.5x - 2.0x, pitch-preserving (only changes GStreamer's `pitch`
        element's `tempo` property, not `pitch`, so playback speeds up/down
        without the chipmunk/demon voice effect)."""
        self.speed = max(0.5, min(2.0, tempo))
        if self.pitch_elem is not None:
            self.pitch_elem.set_property("tempo", self.speed)
        self.emit("dsp-changed")

    def set_mono_downmix(self, enabled: bool):
        self.mono_downmix = enabled
        caps = "audio/x-raw,channels=1" if enabled else "audio/x-raw"
        self.monocaps.set_property("caps", Gst.Caps.from_string(caps))
        self.emit("dsp-changed")

    def set_balance(self, value: float):
        """-1.0 (full left) .. 1.0 (full right). Ignored while 8D audio is
        active (it drives this same property itself); stored so it's
        restored the moment 8D turns off."""
        self.balance = max(-1.0, min(1.0, value))
        if self.panorama_elem is not None and not self.eightd_enabled:
            self.panorama_elem.set_property("panorama", self.balance)
        self.emit("dsp-changed")

    def set_8d_audio(self, enabled: bool, period_seconds: float = 8.0):
        """8D audio: sweep the stereo pan back and forth, duck the volume
        at the extremes, AND low-pass filter it there too - muffling is
        what actually sells "moving away/behind you" to your ears; volume
        and pan alone still just reads as flat left-right panning."""
        self.eightd_enabled = enabled
        if self.panorama_elem is None:
            self.emit("dsp-changed")
            return

        depth_amount = 0.35
        cutoff_near = 20000.0   # centered: full brightness, unfiltered
        cutoff_far = 1500.0     # at the extremes: noticeably muffled

        if enabled:
            if self._eightd_timeout_id is None:
                self._eightd_start_time = time.time()

                def tick():
                    if not self.eightd_enabled:
                        return False
                    elapsed = time.time() - self._eightd_start_time
                    phase = 2 * math.pi * elapsed / period_seconds
                    pan = math.sin(phase)
                    depth = abs(pan)  # 0 = centered, 1 = fully to one side
                    self.panorama_elem.set_property("panorama", pan)
                    if self.depth_volume is not None:
                        self.depth_volume.set_property("volume", 1.0 - depth_amount * depth)
                    if self.depth_filter is not None:
                        cutoff = cutoff_near - (cutoff_near - cutoff_far) * depth
                        self.depth_filter.set_property("cutoff", cutoff)
                    return True

                self._eightd_timeout_id = GLib.timeout_add(50, tick)
        else:
            if self._eightd_timeout_id is not None:
                GLib.source_remove(self._eightd_timeout_id)
                self._eightd_timeout_id = None
            self.panorama_elem.set_property("panorama", self.balance)
            if self.depth_volume is not None:
                self.depth_volume.set_property("volume", 1.0)
            if self.depth_filter is not None:
                self.depth_filter.set_property("cutoff", cutoff_near)
        self.emit("dsp-changed")

    def set_concert_hall(self, enabled: bool):
        """Concert Hall mode: static room reverb + headphone crossfeed, no
        panning motion. Independent of 8D audio - the two don't touch the
        same properties (8D drives panorama/depth_volume/depth_filter;
        this drives reverb/bs2b), so leaving both on isn't a conflict, just
        a lot at once."""
        self.concert_hall_enabled = enabled
        if self.reverb_elem is not None:
            # 0.42 wet: with room-size/damping this large, much past this
            # starts smearing transients instead of just surrounding them.
            self.reverb_elem.set_property("level", 0.42 if enabled else 0.0)
        if self.bs2b_elem is not None:
            self.bs2b_elem.set_property("feed", 60 if enabled else 10)  # 6dB, bs2b "cmoy" preset - stronger crossfeed
        self.emit("dsp-changed")

    def set_gapless(self, enabled: bool):
        self.gapless_enabled = enabled

    # --- Queue / library navigation ---
    def set_queue(self, filepaths: list[str], start_index: int = 0):
        self.queue = list(filepaths)
        self.queue_index = max(0, min(start_index, len(self.queue) - 1)) if self.queue else -1
        self.history = []
        self._gapless_pending_path = None
        self.emit("queue-changed")

    def play_at_index(self, index: int):
        if 0 <= index < len(self.queue):
            self._gapless_pending_path = None
            self.history.append(self.queue_index)
            self.queue_index = index
            self.load_and_play(self.queue[index])
            self.emit("queue-changed")

    def _decide_next_index(self):
        """Mutates queue_index/history to whatever should play next and
        returns that path, or None if there's nothing left to play (queue
        empty, or end reached with repeat off). Shared by the manual "skip"
        path (next_track) and the gapless about-to-finish path, so both
        commit to the exact same decision logic - important for shuffle,
        where "peek then re-roll" would pick two different tracks."""
        if not self.queue:
            return None
        if self.repeat == REPEAT_ONE:
            return self.queue[self.queue_index]
        self.history.append(self.queue_index)
        if self.shuffle:
            if len(self.queue) > 1:
                choices = [i for i in range(len(self.queue)) if i != self.queue_index]
                self.queue_index = (self._weighted_choice(choices) if self.smart_shuffle
                                     else random.choice(choices))
            return self.queue[self.queue_index]
        self.queue_index += 1
        if self.queue_index >= len(self.queue):
            if self.repeat == REPEAT_ALL:
                self.queue_index = 0
            else:
                self.queue_index = len(self.queue) - 1
                return None
        return self.queue[self.queue_index]

    def next_track(self):
        """Manual/explicit skip - always a hard cut, even if gapless already
        had something lined up (an explicit skip supersedes it)."""
        self._gapless_pending_path = None
        next_path = self._decide_next_index()
        if next_path is None:
            self.stop()
            self.emit("queue-changed")
            return
        self.load_and_play(next_path)
        self.emit("queue-changed")

    def _weighted_choice(self, choices: list) -> int:
        """Smart shuffle: weight toward tracks not played recently, instead
        of pure random. Never-played tracks get the highest weight."""
        history = play_history.load()
        now = time.time()
        weights = []
        for i in choices:
            path = self.queue[i]
            last = history.get(path, 0)
            age_days = (now - last) / 86400 if last else 3650  # never played -> "10 years ago"
            weights.append(min(age_days, 3650) + 1)  # +1 so nothing has zero weight
        return random.choices(choices, weights=weights, k=1)[0]

    def prev_track(self):
        if not self.queue:
            return
        self._gapless_pending_path = None
        # if we're more than 3s into the track, restart it instead of going back
        if self.get_position_ms() > 3000:
            self.seek(0)
            return
        if self.shuffle and self.history:
            self.queue_index = self.history.pop()
            self.load_and_play(self.queue[self.queue_index])
            self.emit("queue-changed")
            return
        self.queue_index -= 1
        if self.queue_index < 0:
            self.queue_index = 0 if self.repeat != REPEAT_ALL else len(self.queue) - 1
        self.load_and_play(self.queue[self.queue_index])
        self.emit("queue-changed")

    def set_shuffle(self, enabled: bool):
        self.shuffle = enabled
        self.emit("settings-changed")

    def set_smart_shuffle(self, enabled: bool):
        self.smart_shuffle = enabled
        self.emit("settings-changed")

    def insert_next(self, filepath: str):
        """Queue a track to play right after the current one. If nothing is
        currently active (e.g. right after clear_queue()), there's no
        "current track" to insert after - so just start playing it, since
        that's what "Play Next" means when the queue was empty."""
        if self.queue_index < 0:
            self.queue = [filepath]
            self.queue_index = 0
            self.load_and_play(filepath)
            return
        insert_at = self.queue_index + 1
        self.queue.insert(insert_at, filepath)
        self.emit("queue-changed")

    def append_to_queue(self, filepath: str):
        self.queue.append(filepath)
        self.emit("queue-changed")

    def clear_queue(self):
        self.queue = []
        self.queue_index = -1
        self.history = []
        self._gapless_pending_path = None
        self.stop()
        self.emit("queue-changed")

    def set_repeat(self, mode: str):
        if mode in (REPEAT_OFF, REPEAT_ONE, REPEAT_ALL):
            self.repeat = mode
            self.emit("settings-changed")

    def cycle_repeat(self) -> str:
        order = [REPEAT_OFF, REPEAT_ALL, REPEAT_ONE]
        self.repeat = order[(order.index(self.repeat) + 1) % len(order)]
        self.emit("settings-changed")
        return self.repeat

    # --- Internal ---
    def _ensure_position_timer(self):
        if self._position_timeout_id is None:
            self._position_timeout_id = GLib.timeout_add(250, self._on_position_tick)

    def _on_position_tick(self):
        if self.current_uri:
            pos = self.get_position_ms()
            dur = self.get_duration_ms()
            self.emit("position-changed", pos, dur)
        return True  # keep going

    def _on_about_to_finish(self, playbin):
        """Gapless hook - fires shortly before the current stream ends.
        Called from GStreamer's streaming thread, not the main thread; only
        touches plain GStreamer element properties (thread-safe) and simple
        Python attributes (queue_index/history - fine under the GIL for
        this kind of use)."""
        if not self.gapless_enabled:
            return
        next_path = self._decide_next_index()
        if next_path is None:
            return  # end of queue - let it end normally, no gapless target
        try:
            uri = Gst.filename_to_uri(next_path)
        except Exception as e:
            print(f"[player] gapless: bad path {next_path}: {e}")
            return
        playbin.set_property("uri", uri)
        self._gapless_pending_path = next_path
        self.current_path = next_path
        self.current_uri = uri
        play_history.record_play(next_path)

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            self.emit("eos")
            if self._gapless_pending_path is None:
                self.next_track()
            # else: gapless already committed to the next track via
            # about-to-finish; STREAM_START below announces it to the UI.
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[player] GStreamer error: {err} ({debug})")
            self.emit("state-changed", "error")
        elif t == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct and struct.get_name() == "spectrum":
                mags = None
                try:
                    ok, arr = struct.get_list("magnitude")
                    if ok and arr is not None:
                        mags = [arr.get_nth(i) for i in range(arr.n_values)]
                except Exception:
                    pass
                if mags is None:
                    try:
                        mags = struct.get_value("magnitude")
                    except Exception:
                        pass
                if mags:
                    self.emit("spectrum", list(mags))
        elif t == Gst.MessageType.STREAM_START:
            if self._gapless_pending_path is not None:
                path = self._gapless_pending_path
                self._gapless_pending_path = None
                self.duration_ms = 0
                self.emit("track-changed", path)
                self.emit("state-changed", "playing")
        elif t == Gst.MessageType.ASYNC_DONE:
            # Pipeline finished a state transition (e.g. prerolling after a
            # track load). Re-announce our intended state so MPRIS consumers
            # that queried mid-transition don't stay stuck on a stale value.
            if message.src == self.pipeline:
                self.emit("state-changed", "playing" if self._desired_playing else "paused")
