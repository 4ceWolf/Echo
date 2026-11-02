"""Queue view with right-click context menu, move up/down, remove, and clear animation."""
import concurrent.futures
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GObject, GdkPixbuf, Gdk, GLib

import art_utils

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

_thumb_cache: dict = {}
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="EchoQueueThumb")


class QueueEntry(GObject.Object):
    __gtype_name__ = "QueueEntry"
    is_playing = GObject.Property(type=bool, default=False)
    title = GObject.Property(type=str, default="")
    artist = GObject.Property(type=str, default="")

    def __init__(self, path: str, index: int):
        super().__init__()
        self.path = path
        self.index = index
        self.display_name = Path(path).stem
        self.title = self.display_name
        self.artist = ""

        if " - " in self.display_name:
            parts = self.display_name.split(" - ", 1)
            self.artist = parts[0].strip()
            self.title = parts[1].strip()


class QueueView(Gtk.Box):
    __gsignals__ = {
        "jump-to-index": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "clear-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "open-file-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "open-folder-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "move-up-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "move-down-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "remove-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_vexpand(True)
        self.set_hexpand(True)

        self.store = Gio.ListStore(item_type=QueueEntry)
        self.selection_model = Gtk.SingleSelection(model=self.store)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)

        self.list_view = Gtk.ListView(model=self.selection_model, factory=factory)
        self.list_view.connect("activate", self._on_activate)
        self.list_view.add_css_class("navigation-sidebar")

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self.list_view)

        # Empty state
        self.empty_state = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.empty_state.set_valign(Gtk.Align.CENTER)
        self.empty_state.set_halign(Gtk.Align.CENTER)
        self.empty_state.set_vexpand(True)
        self.empty_state.set_margin_top(20)
        self.empty_state.set_margin_bottom(20)

        empty_icon = Gtk.Image.new_from_icon_name("view-continuous-symbolic")
        empty_icon.set_pixel_size(48)
        empty_icon.add_css_class("dim-label")

        empty_label = Gtk.Label(label="Queue is empty")
        empty_label.add_css_class("title-4")

        empty_sub = Gtk.Label(label="Songs you play or add will appear here")
        empty_sub.add_css_class("dim-label")
        empty_sub.add_css_class("caption")

        open_file_btn = Gtk.Button(label="Open File…")
        open_file_btn.connect("clicked", lambda b: self.emit("open-file-requested"))
        open_folder_btn = Gtk.Button(label="Open Folder…")
        open_folder_btn.connect("clicked", lambda b: self.emit("open-folder-requested"))

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
        btn_box.append(open_file_btn)
        btn_box.append(open_folder_btn)

        self.empty_state.append(empty_icon)
        self.empty_state.append(empty_label)
        self.empty_state.append(empty_sub)
        self.empty_state.append(btn_box)

        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_transition_duration(200)
        self.content_stack.set_vexpand(True)
        self.content_stack.add_named(scroller, "list")
        self.content_stack.add_named(self.empty_state, "empty")
        self.append(self.content_stack)

        bottom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bottom_box.set_margin_top(4)
        bottom_box.set_margin_bottom(4)
        bottom_box.set_margin_start(8)
        bottom_box.set_margin_end(8)

        self.status_label = Gtk.Label(label="Queue is empty", xalign=0)
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_hexpand(True)

        self.clear_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Clear queue")
        self.clear_btn.add_css_class("flat")
        self.clear_btn.connect("clicked", lambda b: self.emit("clear-requested"))

        bottom_box.append(self.status_label)
        bottom_box.append(self.clear_btn)
        self.append(bottom_box)

        self._current_queue = []
        self._current_index = -1

    def refresh(self, queue: list, current_index: int):
        self._current_queue = list(queue)
        self._current_index = current_index
        self.store.remove_all()
        for i, path in enumerate(queue):
            self.store.append(QueueEntry(path, i))
        if not queue:
            self.status_label.set_text("Queue is empty")
            self.content_stack.set_visible_child_name("empty")
        else:
            self.status_label.set_text(f"{len(queue)} tracks in queue")
            self.content_stack.set_visible_child_name("list")
        self.set_now_playing_index(current_index)

    def animate_clear(self):
        list_child = self.content_stack.get_child_by_name("list")
        if list_child:
            try:
                import gi
                gi.require_version("Adw", "1")
                from gi.repository import Adw
                target = Adw.CallbackAnimationTarget.new(lambda v: list_child.set_opacity(v))
                anim = Adw.TimedAnimation.new(list_child, 1.0, 0.0, 200, target)

                def on_done(a):
                    list_child.set_opacity(1.0)

                anim.connect("done", on_done)
                anim.play()
            except Exception:
                pass
        GLib.timeout_add(210, lambda: self.content_stack.set_visible_child_name("empty") or False)
        self.status_label.set_text("Queue is empty")

    def set_now_playing_index(self, index: int):
        self._current_index = index
        n = self.store.get_n_items()
        for i in range(n):
            entry: QueueEntry = self.store.get_item(i)
            entry.is_playing = (i == index)
        if 0 <= index < n:
            self.list_view.scroll_to(index, Gtk.ListScrollFlags.NONE, None)

    def _on_factory_setup(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(3)
        box.set_margin_bottom(3)

        num_label = Gtk.Label(xalign=0.5)
        num_label.set_size_request(24, -1)
        num_label.add_css_class("dim-label")
        num_label.add_css_class("caption")
        box.append(num_label)

        cover = Gtk.Picture()
        cover.set_size_request(32, 32)
        cover.set_content_fit(Gtk.ContentFit.COVER)

        fallback = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        fallback.set_pixel_size(18)

        art_stack = Gtk.Stack()
        art_stack.set_size_request(32, 32)
        art_stack.add_named(fallback, "fallback")
        art_stack.add_named(cover, "cover")
        art_stack.set_visible_child_name("fallback")

        cover_frame = Gtk.Frame()
        cover_frame.set_size_request(32, 32)
        cover_frame.set_child(art_stack)
        box.append(cover_frame)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)

        title_label = Gtk.Label(xalign=0)
        title_label.set_ellipsize(3)

        artist_label = Gtk.Label(xalign=0)
        artist_label.set_ellipsize(3)
        artist_label.add_css_class("dim-label")
        artist_label.add_css_class("caption")

        text_box.append(title_label)
        text_box.append(artist_label)
        box.append(text_box)

        list_item.set_child(box)
        list_item._num_label = num_label
        list_item._cover = cover
        list_item._art_stack = art_stack
        list_item._title_label = title_label
        list_item._artist_label = artist_label
        list_item._row_box = box
        list_item._notify_handler = None
        list_item._bound_node = None

        # Right-click context menu
        gesture = Gtk.GestureClick(button=3)

        def on_right_click(gesture_, n_press, x, y, li=list_item):
            entry = li._bound_node
            if entry is None:
                return
            menu = Gio.Menu()
            menu.append("Play Now", "queue-menu.play-now")
            menu.append("Move Up", "queue-menu.move-up")
            menu.append("Move Down", "queue-menu.move-down")
            menu.append("Remove from Queue", "queue-menu.remove")

            ag = Gio.SimpleActionGroup()

            play_action = Gio.SimpleAction.new("play-now", None)
            play_action.connect("activate", lambda a, p, e=entry: self.emit("jump-to-index", e.index))
            ag.add_action(play_action)

            up_action = Gio.SimpleAction.new("move-up", None)
            up_action.connect("activate", lambda a, p, e=entry: self.emit("move-up-requested", e.index))
            ag.add_action(up_action)

            down_action = Gio.SimpleAction.new("move-down", None)
            down_action.connect("activate", lambda a, p, e=entry: self.emit("move-down-requested", e.index))
            ag.add_action(down_action)

            remove_action = Gio.SimpleAction.new("remove", None)
            remove_action.connect("activate", lambda a, p, e=entry: self.emit("remove-requested", e.index))
            ag.add_action(remove_action)

            box.insert_action_group("queue-menu", ag)
            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.set_parent(box)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
            popover.set_has_arrow(True)
            popover.popup()

        gesture.connect("pressed", on_right_click)
        box.add_controller(gesture)

    def _on_factory_bind(self, factory, list_item):
        entry: QueueEntry = list_item.get_item()
        list_item._bound_node = entry
        self._update_row_visual(list_item, entry)
        list_item._notify_handler = entry.connect(
            "notify::is-playing", lambda n, p: self._update_row_visual(list_item, n)
        )
        self._load_thumbnail_async(list_item, entry)

    def _on_factory_unbind(self, factory, list_item):
        if list_item._notify_handler is not None and list_item._bound_node is not None:
            list_item._bound_node.disconnect(list_item._notify_handler)
        list_item._notify_handler = None
        list_item._bound_node = None

    def _update_row_visual(self, list_item, entry: QueueEntry):
        list_item._num_label.set_text(str(entry.index + 1))
        list_item._title_label.set_text(entry.title or entry.display_name)
        if entry.artist:
            list_item._artist_label.set_text(entry.artist)
            list_item._artist_label.set_visible(True)
        else:
            list_item._artist_label.set_visible(False)

        if entry.is_playing:
            list_item._row_box.add_css_class("now-playing-row")
        else:
            list_item._row_box.remove_css_class("now-playing-row")

    def _load_thumbnail_async(self, list_item, entry: QueueEntry):
        key = entry.path
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
                small = pixbuf.scale_simple(32, 32, GdkPixbuf.InterpType.BILINEAR)
                texture = Gdk.Texture.new_for_pixbuf(small)
                GLib.idle_add(self._on_thumb_ready, key, texture, list_item)
            except Exception:
                GLib.idle_add(self._on_thumb_ready, key, False, list_item)

        _executor.submit(worker)

    def _on_thumb_ready(self, key: str, texture_or_false, list_item):
        _thumb_cache[key] = texture_or_false
        if list_item._bound_node is not None and list_item._bound_node.path == key:
            if texture_or_false is False:
                list_item._art_stack.set_visible_child_name("fallback")
            else:
                list_item._cover.set_paintable(texture_or_false)
                list_item._art_stack.set_visible_child_name("cover")
        return False

    def _on_activate(self, list_view, position):
        entry: QueueEntry = self.selection_model.get_item(position)
        self.emit("jump-to-index", entry.index)
