"""Flat, searchable, high-performance view over every track in the library.
Supports multi-token fuzzy search across title, artist, album, and filenames.
Asynchronous thumbnail and metadata loading ensures zero UI stutter.
"""
import concurrent.futures
import os
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GObject, GLib, GdkPixbuf, Gdk

from config import SUPPORTED_EXTENSIONS
import art_utils
import context_menu

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

_thumb_cache: dict = {}
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="EchoThumbWorker")


def format_seconds(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return ""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


def parse_metadata_fast(path: Path) -> dict:
    stem = path.stem
    title = stem
    artist = ""
    album = path.parent.name
    duration_s = 0.0

    if " - " in stem:
        parts = stem.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()

    if MutagenFile is not None:
        try:
            audio = MutagenFile(str(path))
            if audio is not None:
                if hasattr(audio, "info") and hasattr(audio.info, "length"):
                    duration_s = float(audio.info.length)
                if audio.tags:
                    tags = audio.tags
                    for k in ("TIT2", "title", "\xa9nam"):
                        if k in tags:
                            val = tags[k]
                            t_str = str(val[0] if isinstance(val, list) else val).strip()
                            if t_str:
                                title = t_str
                                break
                    for k in ("TPE1", "artist", "\xa9ART"):
                        if k in tags:
                            val = tags[k]
                            a_str = str(val[0] if isinstance(val, list) else val).strip()
                            if a_str:
                                artist = a_str
                                break
                    for k in ("TALB", "album", "\xa9alb"):
                        if k in tags:
                            val = tags[k]
                            al_str = str(val[0] if isinstance(val, list) else val).strip()
                            if al_str:
                                album = al_str
                                break
        except Exception:
            pass

    return {
        "title": title or stem,
        "artist": artist,
        "album": album,
        "duration_s": duration_s,
        "duration_str": format_seconds(duration_s),
    }


class TrackEntry(GObject.Object):
    __gtype_name__ = "TrackEntry"
    is_playing = GObject.Property(type=bool, default=False)
    title = GObject.Property(type=str, default="")
    artist = GObject.Property(type=str, default="")
    album = GObject.Property(type=str, default="")
    duration_str = GObject.Property(type=str, default="")

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.title = path.stem
        self.artist = ""
        self.album = path.parent.name
        self.duration_str = ""
        self.search_key = f"{path.stem} {path.parent.name}".lower()

    def update_metadata(self, meta: dict):
        self.title = meta["title"]
        self.artist = meta["artist"]
        self.album = meta["album"]
        self.duration_str = meta["duration_str"]
        self.search_key = f"{self.title} {self.artist} {self.album} {self.path.stem} {self.path.parent.name}".lower()


class LibraryBrowser(Gtk.Box):
    __gsignals__ = {
        "track-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "tracks-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, int)),  # (list[str], int start_idx)
        "folder-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "play-next-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "add-to-end-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "choose-folder-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, root_path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_vexpand(True)
        self.set_hexpand(True)

        self.root_path = Path(root_path)
        self._node_cache: dict[str, TrackEntry] = {}
        self._playing_path: str = None
        self._rescan_gen = 0

        # Header bar / search input
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_box.set_margin_top(6)
        search_box.set_margin_bottom(6)
        search_box.set_margin_start(8)
        search_box.set_margin_end(8)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search title, artist, album…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_box.append(self.search_entry)
        self.append(search_box)

        self.all_store = Gio.ListStore(item_type=TrackEntry)
        self.filter = Gtk.CustomFilter.new(self._filter_func, None)
        self.filter_model = Gtk.FilterListModel.new(self.all_store, self.filter)
        self.selection_model = Gtk.SingleSelection(model=self.filter_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)

        self.list_view = Gtk.ListView(model=self.selection_model, factory=factory)
        self.list_view.connect("activate", self._on_activate)
        self.list_view.add_css_class("navigation-sidebar")

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_vexpand(True)
        self.scroller.set_child(self.list_view)

        # Empty state
        self.empty_state = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.empty_state.set_valign(Gtk.Align.CENTER)
        self.empty_state.set_halign(Gtk.Align.CENTER)
        self.empty_state.set_vexpand(True)
        self.empty_state.set_margin_top(20)
        self.empty_state.set_margin_bottom(20)

        empty_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        empty_icon.set_pixel_size(48)
        empty_icon.add_css_class("dim-label")

        self.empty_label = Gtk.Label(label="No songs found")
        self.empty_label.add_css_class("title-4")

        self.empty_sublabel = Gtk.Label(label="Select a music folder with audio tracks")
        self.empty_sublabel.add_css_class("dim-label")
        self.empty_sublabel.add_css_class("caption")

        change_folder_btn = Gtk.Button(label="Choose Music Folder…")
        change_folder_btn.add_css_class("suggested-action")
        change_folder_btn.connect("clicked", lambda b: self.emit("choose-folder-requested"))

        self.empty_state.append(empty_icon)
        self.empty_state.append(self.empty_label)
        self.empty_state.append(self.empty_sublabel)
        self.empty_state.append(change_folder_btn)

        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_transition_duration(180)
        self.content_stack.set_vexpand(True)
        self.content_stack.add_named(self.scroller, "list")
        self.content_stack.add_named(self.empty_state, "empty")
        self.append(self.content_stack)

        # Bottom status bar
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.set_margin_top(4)
        status_bar.set_margin_bottom(4)
        status_bar.set_margin_start(8)
        status_bar.set_margin_end(8)

        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_hexpand(True)
        status_bar.append(self.status_label)

        folder_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Change Music Folder")
        folder_btn.add_css_class("flat")
        folder_btn.connect("clicked", lambda b: self.emit("choose-folder-requested"))
        status_bar.append(folder_btn)

        self.append(status_bar)

        self.rescan()

    def rescan(self):
        self._rescan_gen += 1
        gen = self._rescan_gen
        self.all_store.remove_all()
        self._node_cache = {}

        if not self.root_path.exists():
            self._update_empty_state(0, is_missing=True)
            return

        tracks_found: list[Path] = []
        try:
            for dirpath, _dirnames, filenames in os.walk(self.root_path):
                for fn in sorted(filenames, key=str.lower):
                    p = Path(dirpath) / fn
                    if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                        tracks_found.append(p)
        except Exception as e:
            print(f"[library] error scanning {self.root_path}: {e}")

        for p in tracks_found:
            entry = TrackEntry(p)
            self.all_store.append(entry)
            self._node_cache[str(p)] = entry

        total_count = len(tracks_found)
        self._update_empty_state(total_count)
        self._update_status_label()

        # Background metadata worker
        def metadata_worker():
            for p in tracks_found:
                if gen != self._rescan_gen:
                    return
                meta = parse_metadata_fast(p)
                p_str = str(p)

                def apply_meta(path_str=p_str, m=meta, g=gen):
                    if g == self._rescan_gen:
                        node = self._node_cache.get(path_str)
                        if node is not None:
                            node.update_metadata(m)
                    return False

                GLib.idle_add(apply_meta)

        _executor.submit(metadata_worker)

    def _update_empty_state(self, count: int, is_missing: bool = False):
        if count == 0:
            if is_missing:
                self.empty_label.set_text("Folder Not Found")
                self.empty_sublabel.set_text(f"{self.root_path} does not exist")
            elif self.search_entry.get_text().strip():
                self.empty_label.set_text("No Matching Songs")
                self.empty_sublabel.set_text("Try a different search query")
            else:
                self.empty_label.set_text("No Songs in Library")
                self.empty_sublabel.set_text(f"No audio files found in {self.root_path.name}")
            self.content_stack.set_visible_child_name("empty")
        else:
            self.content_stack.set_visible_child_name("list")

    def _update_status_label(self):
        total = self.all_store.get_n_items()
        filtered = self.filter_model.get_n_items()
        query = self.search_entry.get_text().strip()
        if query:
            self.status_label.set_text(f"Showing {filtered} of {total} songs")
            self._update_empty_state(filtered)
        else:
            self.status_label.set_text(f"{total} songs")
            self._update_empty_state(total)

    def _filter_func(self, item: TrackEntry, user_data):
        query = self.search_entry.get_text().strip().lower()
        if not query:
            return True
        terms = query.split()
        key = item.search_key
        return all(term in key for term in terms)

    def _on_search_changed(self, entry):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_status_label()

    def focus_search(self):
        self.search_entry.grab_focus()

    def _on_factory_setup(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        # Thumbnail with fallback
        cover = Gtk.Picture()
        cover.set_size_request(36, 36)
        cover.set_content_fit(Gtk.ContentFit.COVER)

        fallback_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        fallback_icon.set_pixel_size(20)

        art_stack = Gtk.Stack()
        art_stack.set_size_request(36, 36)
        art_stack.add_named(fallback_icon, "fallback")
        art_stack.add_named(cover, "cover")
        art_stack.set_visible_child_name("fallback")

        cover_frame = Gtk.Frame()
        cover_frame.set_size_request(36, 36)
        cover_frame.add_css_class("library-thumb-frame")
        cover_frame.set_child(art_stack)
        box.append(cover_frame)

        # Text labels
        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        label_box.set_hexpand(True)

        title_label = Gtk.Label(xalign=0)
        title_label.set_ellipsize(3)
        title_label.add_css_class("heading")

        subtitle_label = Gtk.Label(xalign=0)
        subtitle_label.set_ellipsize(3)
        subtitle_label.add_css_class("dim-label")
        subtitle_label.add_css_class("caption")

        label_box.append(title_label)
        label_box.append(subtitle_label)
        box.append(label_box)

        # Duration label
        dur_label = Gtk.Label(xalign=1)
        dur_label.add_css_class("dim-label")
        dur_label.add_css_class("caption")
        box.append(dur_label)

        list_item.set_child(box)
        list_item._cover = cover
        list_item._art_stack = art_stack
        list_item._title_label = title_label
        list_item._subtitle_label = subtitle_label
        list_item._dur_label = dur_label
        list_item._row_box = box
        list_item._handlers = []
        list_item._bound_node = None

        context_menu.attach_track_context_menu(
            box,
            get_filepath_at=lambda: (
                str(list_item._bound_node.path) if list_item._bound_node else None
            ),
            on_play_next=lambda path: self.emit("play-next-requested", path),
            on_add_to_end=lambda path: self.emit("add-to-end-requested", path),
        )

    def _on_factory_bind(self, factory, list_item):
        entry: TrackEntry = list_item.get_item()
        list_item._bound_node = entry

        self._update_row_visual(list_item, entry)

        # Connect property change notifications
        h1 = entry.connect("notify::is-playing", lambda n, p: self._update_row_visual(list_item, n))
        h2 = entry.connect("notify::title", lambda n, p: self._update_row_visual(list_item, n))
        h3 = entry.connect("notify::artist", lambda n, p: self._update_row_visual(list_item, n))
        h4 = entry.connect("notify::duration-str", lambda n, p: self._update_row_visual(list_item, n))
        list_item._handlers = [h1, h2, h3, h4]

        self._load_thumbnail_async(list_item, entry)

    def _on_factory_unbind(self, factory, list_item):
        if list_item._bound_node is not None and list_item._handlers:
            for h in list_item._handlers:
                list_item._bound_node.disconnect(h)
        list_item._handlers = []
        list_item._bound_node = None

    def _update_row_visual(self, list_item, entry: TrackEntry):
        list_item._title_label.set_text(entry.title or entry.path.stem)
        sub = entry.artist if entry.artist else entry.album
        if entry.artist and entry.album and entry.album != entry.path.parent.name:
            sub = f"{entry.artist} • {entry.album}"
        list_item._subtitle_label.set_text(sub)
        list_item._dur_label.set_text(entry.duration_str)

        if entry.is_playing:
            list_item._row_box.add_css_class("now-playing-row")
        else:
            list_item._row_box.remove_css_class("now-playing-row")

    def _load_thumbnail_async(self, list_item, entry: TrackEntry):
        key = str(entry.path)
        cached = _thumb_cache.get(key)
        if cached is not None:
            if cached is False:
                list_item._art_stack.set_visible_child_name("fallback")
            else:
                list_item._cover.set_paintable(cached)
                list_item._art_stack.set_visible_child_name("cover")
            return

        list_item._art_stack.set_visible_child_name("fallback")

        def worker():
            data = art_utils.extract_embedded_art(key)
            if not data:
                GLib.idle_add(self._on_thumb_ready, key, False, list_item)
                return
            pixbuf = art_utils.pixbuf_from_bytes(data)
            if pixbuf is None:
                GLib.idle_add(self._on_thumb_ready, key, False, list_item)
                return
            try:
                small = pixbuf.scale_simple(36, 36, GdkPixbuf.InterpType.BILINEAR)
                texture = Gdk.Texture.new_for_pixbuf(small)
                GLib.idle_add(self._on_thumb_ready, key, texture, list_item)
            except Exception:
                GLib.idle_add(self._on_thumb_ready, key, False, list_item)

        _executor.submit(worker)

    def _on_thumb_ready(self, key: str, texture_or_false, list_item):
        _thumb_cache[key] = texture_or_false
        if list_item._bound_node is not None and str(list_item._bound_node.path) == key:
            if texture_or_false is False:
                list_item._art_stack.set_visible_child_name("fallback")
            else:
                list_item._cover.set_paintable(texture_or_false)
                list_item._art_stack.set_visible_child_name("cover")
        return False

    def _on_activate(self, list_view, position):
        n_items = self.filter_model.get_n_items()
        if position >= n_items:
            return
        track_paths = []
        clicked_path = None
        for i in range(n_items):
            item: TrackEntry = self.filter_model.get_item(i)
            p = str(item.path)
            track_paths.append(p)
            if i == position:
                clicked_path = p

        if clicked_path:
            self.emit("track-activated", clicked_path)
            self.emit("tracks-activated", track_paths, position)

    def set_now_playing(self, filepath: str):
        if self._playing_path and self._playing_path in self._node_cache:
            self._node_cache[self._playing_path].is_playing = False
        self._playing_path = filepath
        node = self._node_cache.get(filepath)
        if node is not None:
            node.is_playing = True

    def set_root(self, root_path: str):
        self.root_path = Path(root_path)
        self.rescan()
