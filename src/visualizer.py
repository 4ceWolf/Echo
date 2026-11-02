"""Bar-style spectrum visualizer rendered via Gtk.DrawingArea + Cairo.
Fixed height — never expands vertically regardless of layout changes.
Rendered with rounded bar caps, subtle dynamic gradients, and smooth auto-gain.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

try:
    import cairo
except ImportError:
    cairo = None


FIXED_HEIGHT = 110  # px — set_content_height + set_vexpand(False) keeps it locked


class Visualizer(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_vexpand(False)
        self.set_hexpand(True)
        self.set_content_height(FIXED_HEIGHT)
        self.set_draw_func(self._draw, None)
        self._magnitudes = []
        self._smoothed = []
        self._decay = 0.80

        self._base_color = (0.55, 0.45, 0.85)
        self._dynamic_enabled = True
        self._dynamic_color = None
        self._recent_peak = 0.3

    def update_spectrum(self, magnitudes: list):
        normalized = [max(0.0, min(1.0, (m + 70.0) / 70.0)) for m in magnitudes]
        self._apply_normalized(normalized)

    def update_spectrum_normalized(self, values: list):
        normalized = [max(0.0, min(1.0, v)) for v in values]
        self._apply_normalized(normalized)

    def _apply_normalized(self, normalized: list):
        if normalized:
            frame_peak = max(normalized)
            self._recent_peak = max(frame_peak, self._recent_peak * 0.995)
            self._recent_peak = max(self._recent_peak, 0.15)
            gain = 1.0 / self._recent_peak
            normalized = [min(1.0, v * gain) for v in normalized]

        if not self._smoothed or len(self._smoothed) != len(normalized):
            self._smoothed = list(normalized)
        else:
            self._smoothed = [
                max(n, s * self._decay)
                for n, s in zip(normalized, self._smoothed)
            ]
        self._magnitudes = self._smoothed
        self.queue_draw()

    def set_dynamic_enabled(self, enabled: bool):
        self._dynamic_enabled = enabled
        self.queue_draw()

    def set_dynamic_color(self, rgb):
        self._dynamic_color = rgb
        self.queue_draw()

    def clear(self):
        self._magnitudes = []
        self._smoothed = []
        self._recent_peak = 0.3
        self.queue_draw()

    def _draw(self, area, cr, width, height, user_data):
        if not self._magnitudes:
            return

        if self._dynamic_enabled and self._dynamic_color:
            r, g, b = self._dynamic_color
        else:
            r, g, b = self._base_color

        n = len(self._magnitudes)
        gap = 2.5
        total_gaps = (n - 1) * gap
        bar_w = max(2.0, (width - total_gaps) / n)
        corner_r = min(3.0, bar_w / 2)

        for i, mag in enumerate(self._magnitudes):
            bar_h = max(2.0, mag * (height - 6))
            x = i * (bar_w + gap)
            y = height - bar_h - 2

            if cairo is not None:
                pat = cairo.LinearGradient(x, y, x, y + bar_h)
                pat.add_color_stop_rgba(0.0, min(1.0, r + 0.18), min(1.0, g + 0.18), min(1.0, b + 0.18), 0.95)
                pat.add_color_stop_rgba(1.0, r, g, b, 0.35)
                cr.set_source(pat)
            else:
                cr.set_source_rgba(r, g, b, 0.8)

            self._rounded_rect(cr, x, y, bar_w, bar_h, corner_r)
            cr.fill()

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
        cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
        cr.arc(x + r, y + r, r, 3.14159, 4.71239)
        cr.close_path()
