import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, GObject, GdkPixbuf, Gdk
from pathlib import Path
import time

from filebrowser import FileBrowser
from librarybrowser import LibraryBrowser
from playlistbrowser import PlaylistBrowser
from queueview import QueueView
from visualizer import Visualizer
from waveform_progress import WaveformProgress
import waveform
from player import Player, REPEAT_OFF, REPEAT_ONE, REPEAT_ALL
from mpris import MprisService
from cava_source import CavaSource
import lyrics as lyrics_mod
import art_utils
from config import SUPPORTED_EXTENSIONS, LOGO_PATH
import config as cfg
import play_history

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

_FALLBACK_ACCENT = (0.55, 0.45, 0.85)

# Shuffle states cycling: off -> normal -> smart -> off
SHUFFLE_OFF = 0
SHUFFLE_NORMAL = 1
SHUFFLE_SMART = 2


def ms_to_mmss(ms: int) -> str:
    s = max(0, int(ms / 1000))
    return f"{s // 60}:{s % 60:02d}"


class EchoWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(1150, 720)
        self.set_title("Echo")

        self.cfg = cfg.load_config()
        self.player = Player()
        self.player.connect("position-changed", self._on_position_changed)
        self.player.connect("state-changed", self._on_state_changed)
        self.player.connect("spectrum", self._on_spectrum)
        self.player.connect("eos", self._on_eos)
        self.player.connect("track-changed", self._on_track_changed)
        self.player.connect("settings-changed", self._on_settings_changed)
        self.player.connect("queue-changed", self._on_queue_changed)

        self._shuffle_state = self.cfg.get("shuffle_state", SHUFFLE_OFF)
        self._current_track_path = None
        self._current_lyrics = lyrics_mod.LyricSet([])
        self._seeking = False
        self._lyric_labels = []
        self._lyric_active_index = -1
        self._current_dominant_rgb = None
        self._current_art_pixbuf = None
        self._accent_css_provider = Gtk.CssProvider()
        self._sidebar_visible = self.cfg.get("sidebar_visible", True)
        self._lyrics_enabled = self.cfg.get("lyrics_enabled", True)
        self._show_visualizer = self.cfg.get("show_visualizer", True)
        self._has_ever_played = False
        self._track_change_gen = 0
        self._pre_mute_volume = 100.0

        self._setup_actions()
        self._setup_key_controller()
        self._build_ui()

        # Apply saved player settings
        self._apply_shuffle_state(self._shuffle_state)
        self.player.repeat = self.cfg.get("repeat", REPEAT_OFF)
        self.player.set_volume(self.cfg.get("volume", 1.0))

        # Apply saved DSP settings
        self.player.set_speed(self.cfg.get("speed", 1.0))
        self.player.set_mono_downmix(self.cfg.get("mono_downmix", False))
        self.player.set_balance(self.cfg.get("balance", 0.0))
        self.player.set_8d_audio(self.cfg.get("eightd_enabled", False))
        self.player.set_concert_hall(self.cfg.get("concert_hall_enabled", False))
        self.player.set_gapless(self.cfg.get("gapless_enabled", False))

        self.mpris = MprisService("Echo", self.player, self._get_track_info_for_mpris)
        self.mpris.start()

        self._cava = CavaSource(bars=48, on_data=self._on_cava_data)
        self._cava_active = self._cava.start()

        self._resume_last_track()
        self.connect("close-request", self._on_close)

    # ── Actions & Shortcuts ────────────────────────────────────────────────
    def _setup_actions(self):
        self._add_simple_action("choose-folder", lambda *_: self._open_folder_dialog())
        self._add_simple_action("rescan-library", lambda *_: self._rescan_all())
        self._add_simple_action("shortcuts", lambda *_: self._show_shortcuts_window())
        self._add_simple_action("about", lambda *_: self._show_about_dialog())

        self._add_stateful_action(
            "toggle-dynamic-color",
            self.cfg.get("dynamic_color", True),
            self._on_action_dynamic_color,
        )
        self._add_stateful_action(
            "toggle-glass",
            self.cfg.get("glass_theme", True),
            self._on_action_glass,
        )
        self._add_stateful_action(
            "toggle-visualizer",
            self.cfg.get("show_visualizer", True),
            self._on_action_visualizer,
        )

    def _add_simple_action(self, name: str, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _add_stateful_action(self, name: str, initial_state: bool, callback):
        action = Gio.SimpleAction.new_stateful(
            name, None, GLib.Variant.new_boolean(initial_state)
        )
        action.connect("change-state", callback)
        self.add_action(action)

    def _on_action_dynamic_color(self, action, value):
        new_state = value.get_boolean()
        action.set_state(value)
        self.cfg["dynamic_color"] = new_state
        cfg.save_config(self.cfg)
        self.visualizer.set_dynamic_enabled(new_state)
        self._refresh_theme()

    def _on_action_glass(self, action, value):
        new_state = value.get_boolean()
        action.set_state(value)
        self.cfg["glass_theme"] = new_state
        cfg.save_config(self.cfg)
        self._refresh_theme()

    def _on_action_visualizer(self, action, value):
        new_state = value.get_boolean()
        action.set_state(value)
        self._show_visualizer = new_state
        self.cfg["show_visualizer"] = new_state
        cfg.save_config(self.cfg)
        self.vis_revealer.set_reveal_child(new_state)

    def _setup_key_controller(self):
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        focus = self.get_focus()
        is_text_entry = isinstance(focus, (Gtk.Editable, Gtk.Entry, Gtk.SearchEntry, Gtk.TextView))

        if keyval == Gdk.KEY_space and not is_text_entry:
            self.player.toggle_play_pause()
            return True
        elif keyval == Gdk.KEY_Left and not is_text_entry:
            if ctrl:
                self.player.prev_track()
            else:
                self.player.seek(max(0, self.player.get_position_ms() - 5000))
            return True
        elif keyval == Gdk.KEY_Right and not is_text_entry:
            if ctrl:
                self.player.next_track()
            else:
                dur = self.player.get_duration_ms()
                self.player.seek(min(dur, self.player.get_position_ms() + 5000))
            return True
        elif ctrl and keyval == Gdk.KEY_f:
            self._on_search_button_clicked(None)
            return True
        elif ctrl and keyval == Gdk.KEY_l:
            self.lyrics_toggle_btn.set_active(not self.lyrics_toggle_btn.get_active())
            return True
        elif keyval == Gdk.KEY_F9 or (ctrl and keyval == Gdk.KEY_b):
            self.sidebar_toggle_btn.set_active(not self.sidebar_toggle_btn.get_active())
            return True
        elif ctrl and shift and keyval == Gdk.KEY_O:
            self._open_folder_dialog()
            return True
        elif ctrl and keyval == Gdk.KEY_o:
            self._open_file_dialog()
            return True
        elif keyval == Gdk.KEY_Escape and is_text_entry:
            self.set_focus(None)
            return True
        return False

    def _show_shortcuts_window(self):
        shortcuts = Gtk.ShortcutsWindow()
        shortcuts.set_transient_for(self)
        shortcuts.set_modal(True)

        section = Gtk.ShortcutsSection()
        section.set_visible(True)

        group_playback = Gtk.ShortcutsGroup(title="Playback")
        group_playback.set_visible(True)
        self._add_shortcut(group_playback, "space", "Play / Pause")
        self._add_shortcut(group_playback, "Left / Right", "Seek backward / forward 5s")
        self._add_shortcut(group_playback, "<Primary>Left / <Primary>Right", "Previous / Next song")

        group_nav = Gtk.ShortcutsGroup(title="Navigation")
        group_nav.set_visible(True)
        self._add_shortcut(group_nav, "<Primary>f", "Search songs")
        self._add_shortcut(group_nav, "<Primary>l", "Toggle lyrics")
        self._add_shortcut(group_nav, "F9 / <Primary>b", "Toggle sidebar")
        self._add_shortcut(group_nav, "<Primary>o", "Open audio file")
        self._add_shortcut(group_nav, "<Primary><Shift>o", "Choose music folder")

        section.append(group_playback)
        section.append(group_nav)
        shortcuts.set_child(section)
        shortcuts.present()

    @staticmethod
    def _add_shortcut(group: Gtk.ShortcutsGroup, accel: str, title: str):
        sc = Gtk.ShortcutsShortcut(accelerator=accel, title=title)
        sc.set_visible(True)
        group.append(sc)

    def _show_about_dialog(self):
        about = Adw.AboutDialog()
        about.set_application_name("Echo")
        about.set_developer_name("Echo Team")
        about.set_version("1.2.0")
        about.set_comments("A native, modern GNOME music player with folder browsing, synced lyrics, live visualizer, and MPRIS2 integration.")
        about.set_website("https://github.com/echo/echo")
        if LOGO_PATH.exists():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(LOGO_PATH), 96, 96, True)
                about.set_logo(Gdk.Texture.new_for_pixbuf(pixbuf))
            except Exception:
                pass
        about.present(self)

    # ── Shuffle ────────────────────────────────────────────────────────────
    def _apply_shuffle_state(self, state: int):
        self._shuffle_state = state
        self.player.set_shuffle(state != SHUFFLE_OFF)
        self.player.set_smart_shuffle(state == SHUFFLE_SMART)

    def _cycle_shuffle(self, *_):
        self._shuffle_state = (self._shuffle_state + 1) % 3
        self._apply_shuffle_state(self._shuffle_state)
        self.cfg["shuffle_state"] = self._shuffle_state
        cfg.save_config(self.cfg)
        self._update_shuffle_icon()

    def _update_shuffle_icon(self):
        s = self._shuffle_state
        btn = self.shuffle_btn
        lbl = self.shuffle_sparkle_label

        for cls in ("shuffle-active", "shuffle-smart"):
            btn.remove_css_class(cls)

        if s == SHUFFLE_OFF:
            btn.set_tooltip_text("Shuffle: off")
            lbl.set_visible(False)
        elif s == SHUFFLE_NORMAL:
            btn.add_css_class("shuffle-active")
            btn.set_tooltip_text("Shuffle: on (click for smart shuffle)")
            lbl.set_visible(False)
        else:  # SMART
            btn.add_css_class("shuffle-smart")
            btn.set_tooltip_text("Smart shuffle: on (click to disable)")
            lbl.set_visible(True)

    def _build_dsp_popover(self) -> Gtk.Popover:
        avail = self.player.dsp_available()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_size_request(260, -1)

        title = Gtk.Label(label="Sound")
        title.add_css_class("title-4")
        title.set_xalign(0)
        box.append(title)

        # Playback speed
        speed_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        speed_label = Gtk.Label(label="Playback Speed", xalign=0)
        speed_label.set_hexpand(True)
        self.speed_value_label = Gtk.Label(label=f"{self.cfg.get('speed', 1.0):.2f}×")
        self.speed_value_label.add_css_class("dim-label")
        speed_header.append(speed_label)
        speed_header.append(self.speed_value_label)
        box.append(speed_header)

        self.speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 2.0, 0.05)
        self.speed_scale.set_value(self.cfg.get("speed", 1.0))
        self.speed_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "1×")
        self.speed_scale.set_sensitive(avail["speed"])
        self.speed_scale.connect("value-changed", self._on_speed_changed)
        box.append(self.speed_scale)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # 8D Audio
        eightd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        eightd_label = Gtk.Label(label="8D Audio (Auto-pan)", xalign=0)
        eightd_label.set_hexpand(True)
        self.eightd_switch = Gtk.Switch()
        self.eightd_switch.set_sensitive(avail["balance_8d"])
        self.eightd_switch.set_active(self.cfg.get("eightd_enabled", False))
        self.eightd_switch.connect("state-set", self._on_8d_toggled)
        eightd_row.append(eightd_label)
        eightd_row.append(self.eightd_switch)
        box.append(eightd_row)

        # Balance
        bal_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bal_label = Gtk.Label(label="Stereo Balance", xalign=0)
        bal_label.set_hexpand(True)
        bal_header.append(bal_label)
        box.append(bal_header)

        self.balance_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -1.0, 1.0, 0.1)
        self.balance_scale.set_value(self.cfg.get("balance", 0.0))
        self.balance_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "C")
        self.balance_scale.set_sensitive(avail["balance_8d"] and not self.cfg.get("eightd_enabled", False))
        self.balance_scale.connect("value-changed", self._on_balance_changed)
        box.append(self.balance_scale)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Concert Hall
        concert_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        concert_label = Gtk.Label(label="Concert Hall (Reverb + Crossfeed)", xalign=0)
        concert_label.set_hexpand(True)
        self.concert_switch = Gtk.Switch()
        self.concert_switch.set_sensitive(avail["concert_hall"])
        self.concert_switch.set_active(self.cfg.get("concert_hall_enabled", False))
        self.concert_switch.connect("state-set", self._on_concert_hall_toggled)
        concert_row.append(concert_label)
        concert_row.append(self.concert_switch)
        box.append(concert_row)

        # Mono Downmix
        mono_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mono_label = Gtk.Label(label="Mono Downmix", xalign=0)
        mono_label.set_hexpand(True)
        self.mono_switch = Gtk.Switch()
        self.mono_switch.set_active(self.cfg.get("mono_downmix", False))
        self.mono_switch.connect("state-set", self._on_mono_toggled)
        mono_row.append(mono_label)
        mono_row.append(self.mono_switch)
        box.append(mono_row)

        # Gapless
        gapless_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gapless_label = Gtk.Label(label="Gapless Playback", xalign=0)
        gapless_label.set_hexpand(True)
        self.gapless_switch = Gtk.Switch()
        self.gapless_switch.set_active(self.cfg.get("gapless_enabled", False))
        self.gapless_switch.connect("state-set", self._on_gapless_toggled)
        gapless_row.append(gapless_label)
        gapless_row.append(self.gapless_switch)
        box.append(gapless_row)

        popover = Gtk.Popover()
        popover.set_child(box)
        return popover

    def _on_speed_changed(self, scale):
        value = scale.get_value()
        self.player.set_speed(value)
        self.speed_value_label.set_text(f"{value:.2f}×")
        self.cfg["speed"] = value
        cfg.save_config(self.cfg)

    def _on_8d_toggled(self, switch, state):
        self.player.set_8d_audio(state)
        self.cfg["eightd_enabled"] = state
        cfg.save_config(self.cfg)
        self.balance_scale.set_sensitive(not state)
        return False

    def _on_balance_changed(self, scale):
        value = scale.get_value()
        self.player.set_balance(value)
        self.cfg["balance"] = value
        cfg.save_config(self.cfg)

    def _on_concert_hall_toggled(self, switch, state):
        self.player.set_concert_hall(state)
        self.cfg["concert_hall_enabled"] = state
        cfg.save_config(self.cfg)
        return False

    def _on_mono_toggled(self, switch, state):
        self.player.set_mono_downmix(state)
        self.cfg["mono_downmix"] = state
        cfg.save_config(self.cfg)
        return False

    def _on_gapless_toggled(self, switch, state):
        self.player.set_gapless(state)
        self.cfg["gapless_enabled"] = state
        cfg.save_config(self.cfg)
        return False

    # ── UI build ───────────────────────────────────────────────────────────
    def _build_ui(self):
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        # Logo + title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        logo_img = Gtk.Image()
        if LOGO_PATH.exists():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(LOGO_PATH), 24, 24, True)
                logo_img.set_from_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
            except Exception as e:
                print(f"[ui] failed to load logo: {e}")
        title_label_widget = Gtk.Label(label="Echo")
        title_label_widget.add_css_class("title")
        title_box.append(logo_img)
        title_box.append(title_label_widget)
        header.set_title_widget(title_box)

        # Left side buttons
        self.sidebar_toggle_btn = Gtk.ToggleButton(
            icon_name="view-sidebar-symbolic", tooltip_text="Toggle sidebar (F9 / Ctrl+B)")
        self.sidebar_toggle_btn.set_active(self._sidebar_visible)
        self.sidebar_toggle_btn.connect("toggled", self._on_sidebar_toggle)
        header.pack_start(self.sidebar_toggle_btn)

        self.lyrics_toggle_btn = Gtk.ToggleButton(
            icon_name="media-optical-symbolic", tooltip_text="Toggle lyrics (Ctrl+L)")
        self.lyrics_toggle_btn.set_active(self._lyrics_enabled)
        self.lyrics_toggle_btn.connect("toggled", self._on_lyrics_toggle)
        header.pack_start(self.lyrics_toggle_btn)

        # Right side buttons
        self.search_btn = Gtk.Button(
            icon_name="system-search-symbolic", tooltip_text="Search songs (Ctrl+F)")
        self.search_btn.connect("clicked", self._on_search_button_clicked)
        header.pack_end(self.search_btn)

        self.dsp_btn = Gtk.MenuButton(
            icon_name="media-eq-symbolic", tooltip_text="Sound effects & DSP")
        self.dsp_btn.set_popover(self._build_dsp_popover())
        header.pack_end(self.dsp_btn)

        open_file_btn = Gtk.Button(
            icon_name="document-open-symbolic", tooltip_text="Open audio file (Ctrl+O)")
        open_file_btn.connect("clicked", lambda b: self._open_file_dialog())
        header.pack_end(open_file_btn)

        # Primary menu
        menu = Gio.Menu()
        sec_library = Gio.Menu()
        sec_library.append("Choose Music Folder…", "win.choose-folder")
        sec_library.append("Rescan Library", "win.rescan-library")
        menu.append_section(None, sec_library)

        sec_theme = Gio.Menu()
        sec_theme.append("Match Album Art Color", "win.toggle-dynamic-color")
        sec_theme.append("Frosted Glass Theme", "win.toggle-glass")
        sec_theme.append("Show Spectrum Visualizer", "win.toggle-visualizer")
        menu.append_section("Appearance", sec_theme)

        sec_help = Gio.Menu()
        sec_help.append("Keyboard Shortcuts", "win.shortcuts")
        sec_help.append("About Echo", "win.about")
        menu.append_section(None, sec_help)

        self.primary_menu_btn = Gtk.MenuButton(
            icon_name="open-menu-symbolic", tooltip_text="Main Menu")
        self.primary_menu_btn.set_menu_model(menu)
        header.pack_end(self.primary_menu_btn)

        toolbar_view.add_top_bar(header)

        # ── Paned split ────────────────────────────────────────────────────
        self.split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.split.set_position(310 if self._sidebar_visible else 0)
        self.split.set_hexpand(True)
        self.split.set_vexpand(True)

        # ── Browsers ───────────────────────────────────────────────────────
        root_path = self.cfg.get("library_root", cfg.DEFAULT_MUSIC_DIR)

        self.file_browser = FileBrowser(root_path)
        self.file_browser.connect("folder-tracks-activated", self._on_tracks_activated)
        self.file_browser.connect("play-next-requested", self._on_play_next_requested)
        self.file_browser.connect("add-to-end-requested", self._on_add_to_end_requested)
        self.file_browser.connect("choose-folder-requested", lambda *_: self._open_folder_dialog())

        self.library_browser = LibraryBrowser(root_path)
        self.library_browser.connect("tracks-activated", self._on_tracks_activated)
        self.library_browser.connect("play-next-requested", self._on_play_next_requested)
        self.library_browser.connect("add-to-end-requested", self._on_add_to_end_requested)
        self.library_browser.connect("choose-folder-requested", lambda *_: self._open_folder_dialog())

        self.playlist_browser = PlaylistBrowser(root_path)
        self.playlist_browser.connect("folder-tracks-activated", self._on_tracks_activated)

        self.queue_view = QueueView()
        self.queue_view.connect("jump-to-index", self._on_queue_jump)
        self.queue_view.connect("clear-requested", self._on_queue_clear_requested)
        self.queue_view.connect("open-file-requested", lambda w: self._open_file_dialog())
        self.queue_view.connect("open-folder-requested", lambda w: self._open_folder_dialog())
        self.queue_view.connect("move-up-requested", self._on_queue_move_up)
        self.queue_view.connect("move-down-requested", self._on_queue_move_down)
        self.queue_view.connect("remove-requested", self._on_queue_remove)

        self.browser_stack = Gtk.Stack()
        self.browser_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.browser_stack.set_transition_duration(180)
        self.browser_stack.add_titled(self.file_browser, "folder", "Folders")
        self.browser_stack.add_titled(self.library_browser, "library", "All Songs")
        self.browser_stack.add_titled(self.playlist_browser, "playlists", "Playlists")
        self.browser_stack.add_titled(self.queue_view, "queue", "Queue")
        self.browser_stack.set_vexpand(True)

        self.stats_panel = self._build_stats_panel()

        browser_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        browser_col.set_vexpand(True)
        browser_col.append(self.browser_stack)
        browser_col.append(self.stats_panel)

        # Tab switcher
        switcher_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, homogeneous=True)
        switcher_box.set_margin_top(6)
        switcher_box.set_margin_bottom(6)
        switcher_box.set_margin_start(8)
        switcher_box.set_margin_end(8)
        switcher_box.add_css_class("linked")

        tab_defs = [
            ("folder", "folder-symbolic", "Folders"),
            ("library", "system-search-symbolic", "All Songs & Search"),
            ("playlists", "view-list-symbolic", "Playlists"),
            ("queue", "view-continuous-symbolic", "Queue"),
        ]
        self._tab_buttons = {}
        first_btn = None
        for name, icon, tip in tab_defs:
            btn = Gtk.ToggleButton()
            btn.set_icon_name(icon)
            btn.set_tooltip_text(tip)
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.set_active(name == "library" if not Path(root_path).exists() else name == "folder")
            btn.connect("toggled", self._on_tab_toggled, name)
            switcher_box.append(btn)
            self._tab_buttons[name] = btn

        self.sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.sidebar_box.set_name("dynamic-sidebar")
        self.sidebar_box.set_size_request(310, -1)
        self.sidebar_box.append(switcher_box)
        self.sidebar_box.append(browser_col)
        self.sidebar_box.set_visible(self._sidebar_visible)

        self.split.set_start_child(self.sidebar_box)
        self.split.set_resize_start_child(False)

        # ── Content (right pane) ───────────────────────────────────────────
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.content_box.set_margin_top(14)
        self.content_box.set_margin_bottom(14)
        self.content_box.set_margin_start(16)
        self.content_box.set_margin_end(16)

        # Now-playing header card
        self.np_outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.np_outer.set_size_request(-1, 160)

        self.album_art = Gtk.Picture()
        self.album_art.set_content_fit(Gtk.ContentFit.COVER)

        self.art_frame = Gtk.Frame()
        self.art_frame.set_name("dynamic-art-frame")
        self.art_frame.set_size_request(150, 150)
        self.art_frame.set_child(self.album_art)
        self.art_frame.set_visible(False)

        self.title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.title_box.set_size_request(200, -1)

        self.title_label = Gtk.Label(label="Nothing playing", xalign=0)
        self.title_label.add_css_class("title-1")
        self.title_label.set_ellipsize(3)

        self.artist_label = Gtk.Label(label="", xalign=0)
        self.artist_label.add_css_class("title-4")
        self.artist_label.add_css_class("dim-label")
        self.artist_label.set_ellipsize(3)

        self.album_label = Gtk.Label(label="", xalign=0)
        self.album_label.add_css_class("dim-label")
        self.album_label.add_css_class("caption")
        self.album_label.set_ellipsize(3)

        self.title_box.append(self.title_label)
        self.title_box.append(self.artist_label)
        self.title_box.append(self.album_label)

        self.lyrics_scroll = Gtk.ScrolledWindow()
        self.lyrics_scroll.set_name("dynamic-lyrics-scroll")
        self.lyrics_scroll.set_hexpand(True)
        self.lyrics_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.lyrics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.lyrics_box.set_valign(Gtk.Align.CENTER)
        self.lyrics_box.set_margin_top(12)
        self.lyrics_box.set_margin_bottom(12)
        self.lyrics_box.set_margin_start(16)
        self.lyrics_box.set_margin_end(16)
        self.lyrics_scroll.set_child(self.lyrics_box)
        self.lyrics_scroll.set_visible(False)

        self.content_box.append(self.np_outer)
        self._layout_now_playing()

        # Visualizer
        self.visualizer = Visualizer()
        self.visualizer.set_dynamic_enabled(self.cfg.get("dynamic_color", True))

        self.vis_frame = Gtk.Frame()
        self.vis_frame.set_name("dynamic-vis-frame")
        self.vis_frame.set_vexpand(False)
        self.vis_frame.set_valign(Gtk.Align.START)
        self.vis_frame.set_child(self.visualizer)

        self.vis_revealer = Gtk.Revealer()
        self.vis_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.vis_revealer.set_transition_duration(250)
        self.vis_revealer.set_child(self.vis_frame)
        self.vis_revealer.set_reveal_child(self._show_visualizer)
        self.content_box.append(self.vis_revealer)

        # Waveform progress bar
        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        progress_box.set_margin_top(4)

        self.pos_label = Gtk.Label(label="0:00")
        self.pos_label.add_css_class("numeric")
        self.pos_label.add_css_class("caption")

        self.waveform_bar = WaveformProgress()
        self.waveform_bar.connect("seek-requested", self._on_waveform_seek)

        self.dur_label = Gtk.Label(label="0:00")
        self.dur_label.add_css_class("numeric")
        self.dur_label.add_css_class("caption")

        progress_box.append(self.pos_label)
        progress_box.append(self.waveform_bar)
        progress_box.append(self.dur_label)
        self.content_box.append(progress_box)

        # Transport controls
        controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, halign=Gtk.Align.CENTER)
        controls_box.set_margin_top(4)

        shuffle_overlay = Gtk.Overlay()
        self.shuffle_btn = Gtk.Button(icon_name="media-playlist-shuffle-symbolic")
        self.shuffle_btn.set_name("dynamic-shuffle-btn")
        self.shuffle_btn.connect("clicked", self._cycle_shuffle)
        self.shuffle_sparkle_label = Gtk.Label(label="✦")
        self.shuffle_sparkle_label.set_name("shuffle-sparkle")
        self.shuffle_sparkle_label.set_halign(Gtk.Align.END)
        self.shuffle_sparkle_label.set_valign(Gtk.Align.END)
        self.shuffle_sparkle_label.set_visible(False)
        shuffle_overlay.set_child(self.shuffle_btn)
        shuffle_overlay.add_overlay(self.shuffle_sparkle_label)
        self._update_shuffle_icon()

        prev_btn = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text="Previous (Ctrl+Left)")
        prev_btn.connect("clicked", lambda b: self.player.prev_track())

        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play / Pause (Space)")
        self.play_btn.add_css_class("circular")
        self.play_btn.set_name("dynamic-play-btn")
        self.play_btn.connect("clicked", lambda b: self.player.toggle_play_pause())

        next_btn = Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text="Next (Ctrl+Right)")
        next_btn.connect("clicked", lambda b: self.player.next_track())

        self.repeat_btn = Gtk.Button(icon_name="media-playlist-repeat-symbolic", tooltip_text="Repeat: off")
        self.repeat_btn.set_name("dynamic-repeat-btn")
        self.repeat_btn.connect("clicked", self._on_repeat_clicked)

        controls_box.append(shuffle_overlay)
        controls_box.append(prev_btn)
        controls_box.append(self.play_btn)
        controls_box.append(next_btn)
        controls_box.append(self.repeat_btn)
        self.content_box.append(controls_box)

        # Volume control with mute button and wheel scroll
        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        vol_box.set_margin_bottom(6)

        self.vol_btn = Gtk.Button(icon_name="audio-volume-high-symbolic", tooltip_text="Mute / Unmute")
        self.vol_btn.add_css_class("flat")
        self.vol_btn.connect("clicked", self._on_volume_mute_toggle)

        self.vol_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self.vol_scale.set_name("dynamic-vol-scale")
        self.vol_scale.set_range(0, 100)
        self.vol_scale.set_value(self.cfg.get("volume", 1.0) * 100)
        self.vol_scale.set_size_request(130, -1)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.connect("value-changed", self._on_volume_changed)

        scroll_controller = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_controller.connect("scroll", self._on_volume_scroll)
        vol_box.add_controller(scroll_controller)

        vol_box.append(self.vol_btn)
        vol_box.append(self.vol_scale)
        self.content_box.append(vol_box)

        self.split.set_end_child(self.content_box)
        self.split.set_resize_end_child(True)

        toolbar_view.set_content(self.split)
        self.set_content(toolbar_view)

        self._update_repeat_icon()

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._accent_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._refresh_theme()

    # ── Volume handlers ────────────────────────────────────────────────────
    def _on_volume_changed(self, scale):
        vol = scale.get_value() / 100.0
        self.player.set_volume(vol)
        self.cfg["volume"] = vol
        cfg.save_config(self.cfg)

        if vol <= 0.001:
            self.vol_btn.set_icon_name("audio-volume-muted-symbolic")
        elif vol < 0.33:
            self.vol_btn.set_icon_name("audio-volume-low-symbolic")
        elif vol < 0.66:
            self.vol_btn.set_icon_name("audio-volume-medium-symbolic")
        else:
            self.vol_btn.set_icon_name("audio-volume-high-symbolic")

    def _on_volume_mute_toggle(self, _btn):
        cur = self.vol_scale.get_value()
        if cur > 0:
            self._pre_mute_volume = cur
            self.vol_scale.set_value(0)
        else:
            restore = getattr(self, "_pre_mute_volume", 80.0)
            self.vol_scale.set_value(restore if restore > 0 else 80.0)

    def _on_volume_scroll(self, controller, dx, dy):
        cur = self.vol_scale.get_value()
        new_val = max(0, min(100, cur - dy * 5))
        self.vol_scale.set_value(new_val)
        return True

    # ── Search Button Handler ──────────────────────────────────────────────
    def _on_search_button_clicked(self, _btn):
        if not self._sidebar_visible:
            self.sidebar_toggle_btn.set_active(True)
        self._set_active_sidebar_tab("library")
        self.library_browser.focus_search()

    def _set_active_sidebar_tab(self, name: str):
        if name in self._tab_buttons:
            self._tab_buttons[name].set_active(True)
            self.browser_stack.set_visible_child_name(name)

    # ── Stats panel ────────────────────────────────────────────────────────
    def _build_stats_panel(self):
        self._stats_revealer = Gtk.Revealer()
        self._stats_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._stats_revealer.set_transition_duration(220)
        self._stats_revealer.set_reveal_child(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_top(4)
        inner.set_margin_bottom(4)
        inner.set_margin_start(8)
        inner.set_margin_end(8)

        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr_lbl = Gtk.Label(label="Recently Played", xalign=0)
        hdr_lbl.add_css_class("caption")
        hdr_lbl.set_hexpand(True)
        clr_btn = Gtk.Button(label="Clear")
        clr_btn.add_css_class("destructive-action")
        clr_btn.add_css_class("caption")
        clr_btn.connect("clicked", self._on_clear_history)
        hdr.append(hdr_lbl)
        hdr.append(clr_btn)

        self._stats_list_box = Gtk.ListBox()
        self._stats_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._stats_list_box.add_css_class("boxed-list")

        stats_scroll = Gtk.ScrolledWindow()
        stats_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        stats_scroll.set_min_content_height(60)
        stats_scroll.set_max_content_height(180)
        stats_scroll.set_propagate_natural_height(True)
        stats_scroll.set_child(self._stats_list_box)

        inner.append(hdr)
        inner.append(stats_scroll)
        self._stats_revealer.set_child(inner)

        toggle = Gtk.ToggleButton(label="▾ History")
        toggle.add_css_class("flat")
        toggle.add_css_class("caption")
        toggle.connect("toggled", lambda b: self._stats_revealer.set_reveal_child(b.get_active()))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.append(toggle)
        outer.append(self._stats_revealer)
        return outer

    def _refresh_stats_panel(self):
        child = self._stats_list_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._stats_list_box.remove(child)
            child = nxt

        history = play_history.load()
        if not history:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label="No history yet", xalign=0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            lbl.set_margin_start(6)
            row.set_child(lbl)
            self._stats_list_box.append(row)
            return

        now = time.time()
        for path, ts in sorted(history.items(), key=lambda kv: kv[1], reverse=True)[:30]:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.set_margin_top(3)
            box.set_margin_bottom(3)
            box.set_margin_start(6)
            box.set_margin_end(6)
            name_lbl = Gtk.Label(label=Path(path).stem, xalign=0)
            name_lbl.add_css_class("caption")
            name_lbl.set_ellipsize(3)
            name_lbl.set_hexpand(True)
            age_s = now - ts
            if age_s < 3600:
                age_str = f"{int(age_s//60)}m ago"
            elif age_s < 86400:
                age_str = f"{int(age_s//3600)}h ago"
            else:
                age_str = f"{int(age_s//86400)}d ago"
            age_lbl = Gtk.Label(label=age_str, xalign=1)
            age_lbl.add_css_class("dim-label")
            age_lbl.add_css_class("caption")
            box.append(name_lbl)
            box.append(age_lbl)
            row.set_child(box)
            self._stats_list_box.append(row)

    def _on_clear_history(self, _btn):
        play_history.clear_history()
        self._refresh_stats_panel()

    def _on_tab_toggled(self, button, name):
        if button.get_active():
            self.browser_stack.set_visible_child_name(name)
            if name == "folder":
                self._refresh_stats_panel()

    # ── Layout ─────────────────────────────────────────────────────────────
    def _layout_now_playing(self):
        child = self.np_outer.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.np_outer.remove(child)
            child = nxt

        for w in (self.art_frame, self.title_box, self.lyrics_scroll):
            if w.get_parent() is not None:
                w.unparent()

        left_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        left_group.append(self.art_frame)
        left_group.append(self.title_box)

        show_lyrics = self.lyrics_scroll.get_visible()
        if show_lyrics:
            self.np_outer.set_size_request(-1, 200)
            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_start_child(left_group)
            paned.set_resize_start_child(False)
            paned.set_shrink_start_child(False)
            paned.set_end_child(self.lyrics_scroll)
            paned.set_resize_end_child(True)
            paned.set_position(self.cfg.get("lyrics_pane_position", 360))
            paned.connect("notify::position", self._on_lyrics_pane_moved)
            paned.set_hexpand(True)
            self.np_outer.append(paned)
        else:
            self.np_outer.set_size_request(-1, 160)
            left_group.set_halign(Gtk.Align.CENTER if not self._sidebar_visible else Gtk.Align.START)
            left_group.set_hexpand(True)
            self.np_outer.append(left_group)

        self.np_outer.set_halign(Gtk.Align.FILL)

    def _on_lyrics_pane_moved(self, paned, pspec):
        self.cfg["lyrics_pane_position"] = paned.get_position()
        cfg.save_config(self.cfg)

    def _on_sidebar_toggle(self, button):
        self._sidebar_visible = button.get_active()
        self.cfg["sidebar_visible"] = self._sidebar_visible
        cfg.save_config(self.cfg)

        if self._sidebar_visible:
            self.sidebar_box.set_visible(True)
            self.sidebar_box.set_opacity(0.0)
            t = Adw.CallbackAnimationTarget.new(lambda v: self.sidebar_box.set_opacity(v))
            Adw.TimedAnimation.new(self.sidebar_box, 0.0, 1.0, 200, t).play()
            self.split.set_position(310)
        else:
            t = Adw.CallbackAnimationTarget.new(lambda v: self.sidebar_box.set_opacity(v))
            anim = Adw.TimedAnimation.new(self.sidebar_box, 1.0, 0.0, 180, t)

            def _hide(a):
                self.sidebar_box.set_visible(False)
                self.split.set_position(0)

            anim.connect("done", _hide)
            anim.play()

        self._layout_now_playing()

    def _on_lyrics_toggle(self, button):
        self._lyrics_enabled = button.get_active()
        self.cfg["lyrics_enabled"] = self._lyrics_enabled
        cfg.save_config(self.cfg)
        self._apply_lyrics_visibility()

    def _apply_lyrics_visibility(self):
        effective = self._lyrics_enabled and bool(self._current_lyrics)
        self.lyrics_scroll.set_visible(effective)
        self._layout_now_playing()

    # ── File dialogs & folder picker ───────────────────────────────────────
    def _open_file_dialog(self):
        dialog = Gtk.FileDialog()
        dialog.set_title("Open audio file")
        dialog.open(self, None, self._on_open_file_chosen)

    def _on_open_file_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except Exception:
            return
        if not gfile:
            return
        path = gfile.get_path()
        if not path:
            return
        self.player.set_queue([path], 0)
        self.player.load_and_play(path)
        self.queue_view.refresh(self.player.queue, self.player.queue_index)

    def _open_folder_dialog(self):
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Music Folder")
        dialog.select_folder(self, None, self._on_open_folder_chosen)

    def _on_open_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if not folder:
            return
        folder_path = folder.get_path()
        if not folder_path:
            return
        self.set_library_root(folder_path)

    def set_library_root(self, folder_path: str):
        self.cfg["library_root"] = folder_path
        cfg.save_config(self.cfg)
        self.file_browser.set_root(folder_path)
        self.library_browser.set_root(folder_path)
        self.playlist_browser.set_root(folder_path)
        self._refresh_stats_panel()

    def _rescan_all(self):
        root = self.cfg.get("library_root", cfg.DEFAULT_MUSIC_DIR)
        self.file_browser.set_root(root)
        self.library_browser.rescan()
        self.playlist_browser.rescan()
        self._refresh_stats_panel()

    # ── Browser callbacks ──────────────────────────────────────────────────
    def _on_tracks_activated(self, browser, filepaths: list, start_index: int = 0):
        if not filepaths:
            return
        idx = max(0, min(start_index, len(filepaths) - 1))
        self.player.set_queue(filepaths, idx)
        self.player.load_and_play(filepaths[idx])
        self.queue_view.refresh(self.player.queue, self.player.queue_index)

    def _on_queue_jump(self, queue_view, index):
        self.player.play_at_index(index)

    def _on_queue_changed(self, player):
        self.queue_view.refresh(player.queue, player.queue_index)

    def _on_queue_clear_requested(self, _qv):
        self.player.clear_queue()
        self.queue_view.animate_clear()

    def _on_queue_move_up(self, _qv, index):
        q = self.player.queue
        if index <= 0 or index >= len(q):
            return
        q[index - 1], q[index] = q[index], q[index - 1]
        if self.player.queue_index == index:
            self.player.queue_index = index - 1
        elif self.player.queue_index == index - 1:
            self.player.queue_index = index
        self.player.emit("queue-changed")

    def _on_queue_move_down(self, _qv, index):
        q = self.player.queue
        if index < 0 or index >= len(q) - 1:
            return
        q[index], q[index + 1] = q[index + 1], q[index]
        if self.player.queue_index == index:
            self.player.queue_index = index + 1
        elif self.player.queue_index == index + 1:
            self.player.queue_index = index
        self.player.emit("queue-changed")

    def _on_queue_remove(self, _qv, index):
        q = self.player.queue
        if index < 0 or index >= len(q):
            return
        q.pop(index)
        if self.player.queue_index >= index and self.player.queue_index > 0:
            self.player.queue_index -= 1
        self.player.emit("queue-changed")

    def _on_play_next_requested(self, browser, filepath):
        self.player.insert_next(filepath)

    def _on_add_to_end_requested(self, browser, filepath):
        self.player.append_to_queue(filepath)

    # ── Restore last session ───────────────────────────────────────────────
    def _resume_last_track(self):
        last = self.cfg.get("last_track")
        if not last or not Path(last).exists():
            return
        try:
            p = Path(last)
            siblings = sorted(
                [str(f) for f in p.parent.iterdir()
                 if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS],
                key=lambda s: s.lower(),
            )
            idx = siblings.index(last) if last in siblings else 0
            self.player.set_queue(siblings, idx)
            self.player.load_paused(last)
            self.queue_view.refresh(self.player.queue, self.player.queue_index)
        except Exception as e:
            print(f"[resume] {e}")

    # ── Track changed with crossfade ───────────────────────────────────────
    def _on_track_changed(self, player, filepath):
        was_first = not self._has_ever_played
        self._has_ever_played = True
        self._current_track_path = filepath
        self.cfg["last_track"] = filepath
        cfg.save_config(self.cfg)

        self._track_change_gen = getattr(self, "_track_change_gen", 0) + 1
        gen = self._track_change_gen

        if was_first:
            self._load_metadata(filepath)
            self._load_lyrics(filepath)
            self._sync_browsers(filepath)
            self._fade_in_content()
            if getattr(self, "mpris", None):
                self.mpris.notify_properties_changed()
        else:
            self._crossfade_to(filepath, gen)

        waveform.extract_peaks_async(
            filepath, lambda peaks, g=gen: self._on_waveform_ready(peaks, g))
        self._refresh_stats_panel()

    def _crossfade_to(self, filepath: str, gen: int):
        targets = [w for w in (self.art_frame, self.title_label, self.artist_label, self.album_label)
                   if w.get_visible()]
        if not targets:
            self._load_metadata(filepath)
            self._load_lyrics(filepath)
            self._sync_browsers(filepath)
            if getattr(self, "mpris", None):
                self.mpris.notify_properties_changed()
            return

        remaining = [len(targets)]

        def _one_done(_anim):
            remaining[0] -= 1
            if remaining[0] != 0:
                return
            if gen != self._track_change_gen:
                return
            self._load_metadata(filepath)
            self._load_lyrics(filepath)
            self._sync_browsers(filepath)
            for w in targets:
                if w.get_visible():
                    t = Adw.CallbackAnimationTarget.new(lambda v, ww=w: ww.set_opacity(v))
                    Adw.TimedAnimation.new(w, 0.0, 1.0, 260, t).play()
            if getattr(self, "mpris", None):
                self.mpris.notify_properties_changed()

        for w in targets:
            t = Adw.CallbackAnimationTarget.new(lambda v, ww=w: ww.set_opacity(v))
            anim = Adw.TimedAnimation.new(w, 1.0, 0.0, 160, t)
            anim.connect("done", _one_done)
            anim.play()

    def _sync_browsers(self, filepath: str):
        self.file_browser.set_now_playing(filepath)
        self.library_browser.set_now_playing(filepath)
        if self.player.queue_index >= 0:
            self.queue_view.set_now_playing_index(self.player.queue_index)
        elif filepath in self.player.queue:
            self.queue_view.set_now_playing_index(self.player.queue.index(filepath))

    def _on_waveform_ready(self, peaks, gen: int):
        if gen != getattr(self, "_track_change_gen", gen):
            return False
        self.waveform_bar.set_peaks(peaks)
        return False

    def _on_waveform_seek(self, bar, fraction):
        dur = self.player.get_duration_ms()
        if dur > 0:
            target_ms = int(fraction * dur)
            self.player.seek(target_ms)
            self.mpris.notify_seeked(target_ms)

    # ── Metadata & art ─────────────────────────────────────────────────────
    def _load_metadata(self, filepath):
        p = Path(filepath)
        title = p.stem
        artist = ""
        album = p.parent.name
        art_pixbuf = None
        art_url = None

        if " - " in title:
            parts = title.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        if MutagenFile is not None:
            try:
                audio = MutagenFile(filepath)
                if audio is not None and audio.tags:
                    tags = audio.tags
                    title = self._first_tag(tags, ["TIT2", "title", "\xa9nam"]) or title
                    artist = self._first_tag(tags, ["TPE1", "artist", "\xa9ART"]) or artist
                    album = self._first_tag(tags, ["TALB", "album", "\xa9alb"]) or album
            except Exception as e:
                print(f"[metadata] {e}")

        art_data = art_utils.extract_embedded_art(filepath)
        if art_data:
            art_pixbuf = art_utils.pixbuf_from_bytes(art_data)
            art_url = art_utils.cache_art_bytes(filepath, art_data)

        self.title_label.set_text(title)
        self.artist_label.set_text(artist)
        self.album_label.set_text(album if album and album != p.parent.name else "")
        self.album_label.set_visible(bool(self.album_label.get_text()))

        if art_pixbuf:
            texture = Gdk.Texture.new_for_pixbuf(art_pixbuf)
            self.album_art.set_paintable(texture)
            self.art_frame.set_visible(True)
            self._current_dominant_rgb = self._dominant_color(art_pixbuf)
            self._current_art_pixbuf = art_pixbuf
        else:
            self.album_art.set_paintable(None)
            self.art_frame.set_visible(False)
            self._current_dominant_rgb = None
            self._current_art_pixbuf = None

        self._refresh_theme()
        self._current_metadata = {
            "title": title,
            "artist": artist,
            "album": album,
            "path": filepath,
            "art_url": art_url,
        }

    def _fade_in_widget(self, widget):
        widget.set_opacity(0.0)
        t = Adw.CallbackAnimationTarget.new(lambda v: widget.set_opacity(v))
        Adw.TimedAnimation.new(widget, 0.0, 1.0, 300, t).play()

    def _fade_in_content(self):
        for w in (self.np_outer, self.title_label, self.artist_label):
            self._fade_in_widget(w)

    @staticmethod
    def _dominant_color(pixbuf):
        try:
            small = pixbuf.scale_simple(24, 24, GdkPixbuf.InterpType.BILINEAR)
            nc = small.get_n_channels()
            px = small.get_pixels()
            stride = small.get_rowstride()
            w, h = small.get_width(), small.get_height()
            r_t = g_t = b_t = wt = 0.0
            for y in range(h):
                for x in range(w):
                    o = y * stride + x * nc
                    r, g, b = px[o], px[o + 1], px[o + 2]
                    mx, mn = max(r, g, b), min(r, g, b)
                    li = (mx + mn) / 2 / 255.0
                    if li < 0.06 or li > 0.94:
                        continue
                    sat = 0 if mx == mn else (mx - mn) / (255 - abs(mx + mn - 255))
                    ww = (sat ** 1.5) + 0.05
                    r_t += r * ww
                    g_t += g * ww
                    b_t += b * ww
                    wt += ww
            if wt == 0:
                return None
            return (r_t / wt / 255, g_t / wt / 255, b_t / wt / 255)
        except Exception as e:
            print(f"[accent] {e}")
            return None

    def _get_logo_accent(self):
        if getattr(self, "_logo_accent_rgb", None) is not None:
            return self._logo_accent_rgb
        self._logo_accent_rgb = self._accent_from_file(LOGO_PATH) or _FALLBACK_ACCENT
        return self._logo_accent_rgb

    @classmethod
    def _accent_from_file(cls, path):
        if not path.exists():
            return None
        try:
            return cls._dominant_color(GdkPixbuf.Pixbuf.new_from_file(str(path)))
        except Exception:
            return None

    @staticmethod
    def _first_tag(tags, keys):
        for k in keys:
            try:
                val = tags.get(k)
                if val:
                    return str(val[0]) if isinstance(val, list) else str(val)
            except Exception:
                continue
        return None

    # ── Lyrics ─────────────────────────────────────────────────────────────
    def _load_lyrics(self, filepath):
        self._current_lyrics = lyrics_mod.load_lyrics_for(filepath)
        child = self.lyrics_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.lyrics_box.remove(child)
            child = nxt
        self._lyric_labels = []
        self._lyric_active_index = -1

        if self._current_lyrics and self._current_lyrics.lines:
            for i, line in enumerate(self._current_lyrics.lines):
                label = Gtk.Label(label=line.text or "♪")
                label.set_wrap(True)
                label.set_xalign(0)
                label.add_css_class("dim-label")
                label.add_css_class("caption")
                label.set_opacity(0.0)

                delay = min(i * 15, 300)

                def _fade(lbl=label):
                    t = Adw.CallbackAnimationTarget.new(lambda v, l=lbl: l.set_opacity(v))
                    Adw.TimedAnimation.new(lbl, 0.0, 0.70, 240, t).play()
                    return False

                GLib.timeout_add(delay, _fade)

                if line.time_ms >= 0:
                    click = Gtk.GestureClick()
                    click.connect("released", self._on_lyric_line_clicked, line.time_ms)
                    label.add_controller(click)

                self.lyrics_box.append(label)
                self._lyric_labels.append(label)

        self._apply_lyrics_visibility()

    def _on_lyric_line_clicked(self, gesture, n_press, x, y, time_ms):
        self.player.seek(time_ms)
        if getattr(self, "mpris", None):
            self.mpris.notify_seeked(time_ms)

    def _update_lyric_highlight(self, position_ms):
        if not self._current_lyrics or not self._current_lyrics.is_synced:
            return
        idx = self._current_lyrics.index_at(position_ms)
        if idx == self._lyric_active_index:
            return
        if 0 <= self._lyric_active_index < len(self._lyric_labels):
            old = self._lyric_labels[self._lyric_active_index]
            old.remove_css_class("title-4")
            old.remove_css_class("lyric-active")
            old.add_css_class("dim-label")
            t = Adw.CallbackAnimationTarget.new(lambda v, l=old: l.set_opacity(v))
            Adw.TimedAnimation.new(old, 1.0, 0.60, 200, t).play()
        if 0 <= idx < len(self._lyric_labels):
            new = self._lyric_labels[idx]
            new.remove_css_class("dim-label")
            new.add_css_class("title-4")
            new.add_css_class("lyric-active")
            t = Adw.CallbackAnimationTarget.new(lambda v, l=new: l.set_opacity(v))
            Adw.TimedAnimation.new(new, 0.60, 1.0, 200, t).play()
            self._scroll_lyrics_to(new)
        self._lyric_active_index = idx

    def _scroll_lyrics_to(self, label):
        def do_scroll():
            ok, rect = label.compute_bounds(self.lyrics_box)
            if not ok:
                return False
            adj = self.lyrics_scroll.get_vadjustment()
            vph = self.lyrics_scroll.get_allocated_height()
            target = rect.origin.y + rect.size.height / 2 - vph / 2
            target = max(adj.get_lower(), min(target, adj.get_upper() - adj.get_page_size()))
            start = adj.get_value()
            if abs(start - target) < 2:
                return False
            t = Adw.CallbackAnimationTarget.new(lambda v: adj.set_value(v))
            Adw.TimedAnimation.new(self.lyrics_scroll, start, target, 260, t).play()
            return False

        GLib.idle_add(do_scroll)

    # ── Player signals ─────────────────────────────────────────────────────
    def _on_position_changed(self, player, pos_ms, dur_ms):
        if not self._seeking:
            self.pos_label.set_text(ms_to_mmss(pos_ms))
            self.dur_label.set_text(ms_to_mmss(dur_ms))
            if dur_ms > 0:
                self.waveform_bar.set_progress(pos_ms / dur_ms)
        self._update_lyric_highlight(pos_ms)

    def _on_state_changed(self, player, state):
        if state == "playing":
            self.play_btn.set_icon_name("media-playback-pause-symbolic")
        else:
            self.play_btn.set_icon_name("media-playback-start-symbolic")
            if state in ("stopped", "error"):
                self.visualizer.clear()
                self._reset_now_playing_display()

    def _reset_now_playing_display(self):
        self._current_track_path = None
        self.title_label.set_text("Nothing playing")
        self.artist_label.set_text("")
        self.album_label.set_text("")
        self.album_art.set_paintable(None)
        self.art_frame.set_visible(False)
        self.waveform_bar.set_peaks([])
        self.waveform_bar.set_progress(0.0)
        self.pos_label.set_text("0:00")
        self.dur_label.set_text("0:00")
        self._current_dominant_rgb = None
        self._current_art_pixbuf = None
        self._refresh_theme()

    def _on_spectrum(self, player, magnitudes):
        if not self._cava_active:
            self.visualizer.update_spectrum(magnitudes)

    def _on_cava_data(self, values):
        self.visualizer.update_spectrum_normalized(values)

    def _on_eos(self, player):
        pass

    def _on_settings_changed(self, player):
        self.cfg["shuffle_state"] = self._shuffle_state
        self.cfg["repeat"] = player.repeat
        cfg.save_config(self.cfg)
        self._update_repeat_icon()

    def _on_repeat_clicked(self, button):
        self.player.cycle_repeat()
        self.cfg["repeat"] = self.player.repeat
        cfg.save_config(self.cfg)
        self._update_repeat_icon()

    def _update_repeat_icon(self):
        if not hasattr(self, "repeat_btn"):
            return
        tip = {REPEAT_OFF: "Repeat: off", REPEAT_ALL: "Repeat: all", REPEAT_ONE: "Repeat: one"}[self.player.repeat]
        self.repeat_btn.set_tooltip_text(tip)
        self.repeat_btn.set_icon_name(
            "media-playlist-repeat-song-symbolic" if self.player.repeat == REPEAT_ONE
            else "media-playlist-repeat-symbolic"
        )
        if self.player.repeat == REPEAT_OFF:
            self.repeat_btn.remove_css_class("repeat-active")
        else:
            self.repeat_btn.add_css_class("repeat-active")

    # ── Theme / CSS ────────────────────────────────────────────────────────
    def _refresh_theme(self):
        dynamic = self.cfg.get("dynamic_color", True)
        glass = self.cfg.get("glass_theme", True)

        rgb = (self._current_dominant_rgb if dynamic and self._current_dominant_rgb
               else self._get_logo_accent())

        self.visualizer.set_dynamic_color(self._current_dominant_rgb if dynamic else None)
        self.waveform_bar.set_accent_color(rgb)

        r, g, b = [int(c * 255) for c in rgb]
        r2, g2, b2 = [min(255, int(c * 255) + 30) for c in rgb]

        win_bg = ""
        if glass:
            win_bg = f"""
            window {{
                background-image: radial-gradient(ellipse at 20% 20%,
                    rgba({r},{g},{b}, 0.50) 0%,
                    rgba({max(0,r-30)},{max(0,g-30)},{max(0,b-30)}, 0.28) 45%,
                    rgba(14, 12, 20, 0.94) 85%);
                transition: background-image 350ms ease;
            }}"""
        else:
            win_bg = "window { background-image: unset; }"

        glass_css = ""
        if glass:
            glass_css = f"""
            .background {{ background-color: transparent; }}
            box, viewport {{ background-color: transparent; }}
            scrolledwindow {{ background-color: transparent; }}

            list, listview {{ background-color: transparent; }}
            row {{ background-color: transparent; border-radius: 6px; transition: background-color 150ms ease; }}
            row.activatable:hover {{
                background-color: rgba({r},{g},{b}, 0.16);
            }}
            row:selected, row:selected:hover {{
                background-color: rgba({r},{g},{b}, 0.35);
            }}
            row:selected label {{ color: white; }}

            headerbar {{
                background-color: rgba({r},{g},{b}, 0.20);
                background-image: none;
                box-shadow: none;
                border-bottom: 1px solid rgba(255,255,255,0.10);
                transition: background-color 300ms ease;
            }}

            #dynamic-sidebar {{
                background-color: rgba({r},{g},{b}, 0.12);
                border-right: 1px solid rgba(255,255,255,0.08);
                transition: background-color 300ms ease;
            }}

            #dynamic-art-frame {{
                background-color: rgba({r},{g},{b}, 0.22);
                border: 1px solid rgba(255,255,255,0.18);
                border-radius: 16px;
                box-shadow: 0 8px 24px rgba({r},{g},{b}, 0.25);
                transition: all 300ms ease;
            }}

            #dynamic-vis-frame {{
                background-color: rgba({r},{g},{b}, 0.14);
                border: 1px solid rgba(255,255,255,0.09);
                border-radius: 12px;
                transition: background-color 300ms ease;
            }}

            #dynamic-lyrics-scroll {{
                background-color: rgba({r},{g},{b}, 0.12);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 14px;
                transition: background-color 300ms ease;
            }}

            frame {{
                background-color: rgba({r},{g},{b}, 0.10);
                border-radius: 8px;
                border: 1px solid rgba(255,255,255,0.07);
                transition: background-color 300ms ease;
            }}

            paned > separator {{ background-color: rgba(255,255,255,0.06); }}
            """

        sparkle_css = f"""
        #shuffle-sparkle {{
            font-size: 8px;
            color: rgba({min(255,r+80)},{min(255,g+60)},{min(255,b+100)}, 1.0);
            text-shadow: 0 0 4px rgba({r},{g},{b},0.9);
        }}
        .shuffle-active {{
            background-color: rgba({r},{g},{b}, 0.65);
            color: white;
            border-radius: 6px;
        }}
        .shuffle-smart {{
            background: linear-gradient(135deg,
                rgba({r},{g},{b},0.75),
                rgba({min(255,r+50)},{min(255,g+30)},{min(255,b+80)},0.90));
            color: white;
            border-radius: 6px;
        }}
        """

        css = f"""
        {win_bg}
        #dynamic-play-btn {{
            background-color: rgb({r},{g},{b});
            color: white;
            min-width: 48px;
            min-height: 48px;
            box-shadow: 0 4px 14px rgba({r},{g},{b}, 0.40);
            transition: all 250ms ease;
        }}
        #dynamic-play-btn:hover {{
            background-color: rgb({r2},{g2},{b2});
            box-shadow: 0 6px 18px rgba({r},{g},{b}, 0.55);
        }}
        #dynamic-vol-scale trough highlight {{
            background-color: rgb({r},{g},{b});
            border-radius: 3px;
            transition: background-color 300ms ease;
        }}
        #dynamic-vol-scale slider {{
            background-color: rgb({r},{g},{b});
            transition: background-color 300ms ease;
        }}
        #dynamic-repeat-btn.repeat-active {{
            background-color: rgba({r},{g},{b},0.65);
            color: white;
            border-radius: 6px;
        }}
        .now-playing-row {{
            background-color: rgba({r},{g},{b},0.26);
            border-radius: 8px;
            border-left: 3px solid rgb({r},{g},{b});
            transition: all 200ms ease;
        }}
        .lyric-active {{
            color: rgb({min(255, r + 40)}, {min(255, g + 40)}, {min(255, b + 40)});
            font-weight: bold;
            text-shadow: 0 0 12px rgba({r},{g},{b}, 0.6);
        }}
        .library-thumb-frame {{
            border-radius: 6px;
        }}
        {sparkle_css}
        {glass_css}
        """
        self._accent_css_provider.load_from_data(css.encode())

    # ── MPRIS ──────────────────────────────────────────────────────────────
    def _get_track_info_for_mpris(self):
        if not self._current_track_path:
            return {}
        meta = getattr(self, "_current_metadata", {})
        return {
            "title": meta.get("title", Path(self._current_track_path).stem),
            "artist": meta.get("artist", ""),
            "album": meta.get("album", ""),
            "length_us": self.player.get_duration_ms() * 1000,
            "art_url": meta.get("art_url"),
        }

    def _on_close(self, *args):
        cfg.save_config(self.cfg)
        if getattr(self, "_cava", None):
            self._cava.stop()
        return False
