"""Amberol-style waveform progress bar: draws the track's actual peak data
as bars, colors the played portion in the accent color and the rest dim,
and supports click/drag to seek."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GObject, Adw


class WaveformProgress(Gtk.DrawingArea):
    __gsignals__ = {
        "seek-requested": (GObject.SignalFlags.RUN_FIRST, None, (float,)),  # 0..1 fraction
    }

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_content_height(48)
        self.set_draw_func(self._draw, None)

        self._peaks = []
        self._progress = 0.0  # 0..1
        self._accent = (0.55, 0.45, 0.85)
        self._dragging = False
        self._reveal = 1.0  # 0..1, animates bars growing in on a new track

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_press)
        click.connect("released", self._on_release)
        self.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)

    def set_peaks(self, peaks):
        self._peaks = peaks or []
        # bars grow in from flat rather than snapping straight to full
        # height - this was the "kinda static" complaint.
        self._reveal = 0.0
        target = Adw.CallbackAnimationTarget.new(self._set_reveal)
        anim = Adw.TimedAnimation.new(self, 0.0, 1.0, 500, target)
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        anim.play()

    def _set_reveal(self, value):
        self._reveal = value
        self.queue_draw()

    def set_progress(self, fraction: float):
        self._progress = max(0.0, min(1.0, fraction))
        self.queue_draw()

    def set_accent_color(self, rgb):
        self._accent = rgb
        self.queue_draw()

    def _on_press(self, gesture, n_press, x, y):
        self._dragging = True
        self._seek_to_x(x)

    def _on_release(self, gesture, n_press, x, y):
        self._dragging = False

    def _on_drag_update(self, gesture, offset_x, offset_y):
        ok, start_x, start_y = gesture.get_start_point()
        if ok:
            self._seek_to_x(start_x + offset_x)

    def _seek_to_x(self, x):
        width = self.get_width()
        if width <= 0:
            return
        fraction = max(0.0, min(1.0, x / width))
        self._progress = fraction  # optimistic UI update while dragging
        self.queue_draw()
        self.emit("seek-requested", fraction)

    def _draw(self, area, cr, width, height, data):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()

        if not self._peaks:
            # no waveform yet (still extracting, or extraction failed) -
            # draw a flat dim line so it doesn't look broken/empty
            cr.set_source_rgba(1, 1, 1, 0.15)
            cr.rectangle(0, height / 2 - 1, width, 2)
            cr.fill()
            return

        n = len(self._peaks)
        gap = 2
        bar_width = max(1.5, (width - gap * (n - 1)) / n)
        played_bars = int(self._progress * n)

        r, g, b = self._accent
        for i, peak in enumerate(self._peaks):
            x = i * (bar_width + gap)
            bar_height = max(2, peak * self._reveal * (height - 4))
            y = (height - bar_height) / 2
            if i < played_bars:
                cr.set_source_rgba(r, g, b, 0.95)
            else:
                cr.set_source_rgba(1, 1, 1, 0.22)
            self._rounded_rect(cr, x, y, bar_width, bar_height, min(2, bar_width / 2))
            cr.fill()

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
        cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
        cr.arc(x + r, y + r, r, 3.14159, 4.71239)
        cr.close_path()
