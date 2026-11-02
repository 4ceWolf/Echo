"""Folder-hierarchy file browser with scroll animation, fixed selected-row highlight,
right-click on folders, and search that doesn't queue parent directories."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GObject, GLib, GdkPixbuf, Gdk, Adw
from pathlib import Path

from config import SUPPORTED_EXTENSIONS
import art_utils
import context_menu as ctx_menu


class FileNode(GObject.Object):
    __gtype_name__ = "FileNode"
    is_playing = GObject.Property(type=bool, default=False)

    def __init__(self, path: Path, is_dir: bool):
        super().__init__()
        self.path = path
        self.is_dir = is_dir
        self.name = path.name or str(path)

    def create_children_model(self, node_cache: dict):
        store = Gio.ListStore(item_type=FileNode)
        try:
            entries = sorted(
                self.path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except (PermissionError, FileNotFoundError):
            return store
        for entry in entries:
            if entry.is_dir():
                node = FileNode(entry, True)
                store.append(node)
                node_cache[str(entry)] = node
            elif entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                node = FileNode(entry, False)
                store.append(node)
                node_cache[str(entry)] = node
        return store


class FileBrowser(Gtk.ScrolledWindow):
    __gsignals__ = {
        "track-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "folder-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "folder-tracks-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, int)),
        "play-next-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "add-to-end-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "choose-folder-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, root_path: str):
        super().__init__()
        self.root_path = Path(root_path)
        self.set_vexpand(True)
        self.set_hexpand(True)

        self._node_cache: dict = {}
        self._playing_path: str = None
        self._folder_cover_cache: dict = {}
        self._track_cover_cache: dict = {}
        self._user_collapsed: set = set()
        self._last_scroll_pos: int = None  # track position for smooth scroll animation

        self.root_store = Gio.ListStore(item_type=FileNode)
        root_node = FileNode(self.root_path, True)
        self.root_store.append(root_node)
        self._node_cache[str(self.root_path)] = root_node

        self.tree_model = Gtk.TreeListModel.new(
            self.root_store,
            False,
            False,
            self._create_child_model,
        )

        self.selection_model = Gtk.SingleSelection(model=self.tree_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)

        self.list_view = Gtk.ListView(model=self.selection_model, factory=factory)
        self.list_view.connect("activate", self._on_activate)
        self.list_view.add_css_class("navigation-sidebar")

        self.set_child(self.list_view)

    def _create_child_model(self, item: FileNode):
        if item.is_dir:
            model = item.create_children_model(self._node_cache)
            if model.get_n_items() == 0:
                return None
            return model
        return None

    def _on_factory_setup(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        expander = Gtk.TreeExpander()
        icon = Gtk.Image()
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(3)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.set_margin_start(2)
        box.set_margin_end(4)
        box.append(icon)
        box.append(label)
        expander.set_child(box)
        list_item.set_child(expander)
        list_item._icon = icon
        list_item._label = label
        list_item._row_box = box
        list_item._notify_handler = None
        list_item._bound_node = None

        # Track right-click: Play Next / Add to End
        ctx_menu.attach_track_context_menu(
            box,
            get_filepath_at=lambda: (
                str(list_item._bound_node.path)
                if list_item._bound_node and not list_item._bound_node.is_dir else None
            ),
            on_play_next=lambda path: self.emit("play-next-requested", path),
            on_add_to_end=lambda path: self.emit("add-to-end-requested", path),
        )

        # Folder right-click: queue whole folder (recursive)
        ctx_menu.attach_folder_context_menu(
            box,
            get_folderpath_at=lambda: (
                str(list_item._bound_node.path)
                if list_item._bound_node and list_item._bound_node.is_dir else None
            ),
            on_play_next=lambda path: self.emit("play-next-requested", path),
            on_add_to_end=lambda path: self.emit("add-to-end-requested", path),
        )

    def _on_factory_bind(self, factory, list_item):
        row = list_item.get_item()
        node: FileNode = row.get_item()
        expander = list_item.get_child()
        expander.set_list_row(row)
        list_item._bound_node = node
        self._update_row_visual(list_item, node)
        list_item._notify_handler = node.connect(
            "notify::is-playing", lambda n, p: self._update_row_visual(list_item, n)
        )

    def _on_factory_unbind(self, factory, list_item):
        if list_item._notify_handler is not None and list_item._bound_node is not None:
            list_item._bound_node.disconnect(list_item._notify_handler)
        list_item._notify_handler = None
        list_item._bound_node = None

    def _update_row_visual(self, list_item, node: "FileNode"):
        if node.is_dir:
            texture = self._get_folder_cover_texture(node.path)
            if texture is not None:
                list_item._icon.set_from_paintable(texture)
            else:
                list_item._icon.set_from_icon_name("folder-symbolic")
        else:
            texture = self._get_track_cover_texture(node.path)
            if texture is not None:
                list_item._icon.set_from_paintable(texture)
            else:
                list_item._icon.set_from_icon_name(
                    "media-playback-start-symbolic" if node.is_playing else "audio-x-generic-symbolic"
                )
        list_item._label.set_text(node.name)

        # Selected/playing highlight: accent tint on the ROW background,
        # never on the label's text color, so the song name stays readable.
        if node.is_playing:
            list_item._row_box.add_css_class("now-playing-row")
        else:
            list_item._row_box.remove_css_class("now-playing-row")

    def _get_folder_cover_texture(self, dir_path: Path):
        key = str(dir_path)
        cached = self._folder_cover_cache.get(key)
        if cached is not None:
            return cached if cached is not False else None
        cover_path = art_utils.resolve_folder_cover(dir_path)
        if cover_path is None:
            self._folder_cover_cache[key] = False
            return None
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(cover_path, 32, 32, True)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self._folder_cover_cache[key] = texture
            return texture
        except Exception as e:
            print(f"[filebrowser] failed to load folder cover {cover_path}: {e}")
            self._folder_cover_cache[key] = False
            return None

    def _get_track_cover_texture(self, track_path: Path):
        key = str(track_path)
        cached = self._track_cover_cache.get(key)
        if cached is not None:
            return cached if cached is not False else None
        data = art_utils.extract_embedded_art(key)
        if not data:
            self._track_cover_cache[key] = False
            return None
        pixbuf = art_utils.pixbuf_from_bytes(data)
        if pixbuf is None:
            self._track_cover_cache[key] = False
            return None
        try:
            small = pixbuf.scale_simple(32, 32, GdkPixbuf.InterpType.BILINEAR)
            texture = Gdk.Texture.new_for_pixbuf(small)
            self._track_cover_cache[key] = texture
            return texture
        except Exception as e:
            print(f"[filebrowser] failed to build thumbnail for {key}: {e}")
            self._track_cover_cache[key] = False
            return None

    def _on_activate(self, list_view, position):
        row = self.selection_model.get_item(position)
        node: FileNode = row.get_item()
        if node.is_dir:
            new_expanded = not row.get_expanded()
            row.set_expanded(new_expanded)
            key = str(node.path)
            if new_expanded:
                self._user_collapsed.discard(key)
            else:
                self._user_collapsed.add(key)
        else:
            track_str = str(node.path)
            siblings = sorted(
                [str(p) for p in node.path.parent.iterdir()
                 if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS],
                key=lambda p: p.lower(),
            )
            idx = siblings.index(track_str) if track_str in siblings else 0
            self.emit("folder-tracks-activated", siblings, idx)
            self.emit("track-activated", track_str)

    def set_now_playing(self, filepath: str):
        if self._playing_path and self._playing_path in self._node_cache:
            self._node_cache[self._playing_path].is_playing = False
        self._playing_path = filepath
        node = self._node_cache.get(filepath)
        if node is not None:
            node.is_playing = True
        self.reveal_path(filepath)

    def reveal_path(self, filepath: str):
        target = Path(filepath)
        try:
            rel_parts = target.relative_to(self.root_path).parts
        except ValueError:
            return

        current_dir = self.root_path
        if not self._expand_dir_if_allowed(current_dir):
            return
        for part in rel_parts[:-1]:
            current_dir = current_dir / part
            if not self._expand_dir_if_allowed(current_dir):
                return

        pos = self._find_position_for_path(str(target))
        if pos is not None:
            self.selection_model.set_selected(pos)
            self._smooth_scroll_to(pos)

    def _smooth_scroll_to(self, position: int):
        """Scroll to position with a smooth animation instead of abrupt jump."""
        def do_scroll():
            adj = self.get_vadjustment()
            if adj is None:
                self.list_view.scroll_to(position, Gtk.ListScrollFlags.NONE, None)
                return False

            # Estimate row height (~40px typical)
            row_height = 40
            target_y = position * row_height
            viewport_h = self.get_allocated_height()
            target_val = max(adj.get_lower(),
                             min(target_y - viewport_h / 2,
                                 adj.get_upper() - adj.get_page_size()))
            current_val = adj.get_value()

            if abs(current_val - target_val) < 5:
                return False

            try:
                gi.require_version("Adw", "1")
                from gi.repository import Adw
                scroll_target = Adw.CallbackAnimationTarget.new(lambda v: adj.set_value(v))
                anim = Adw.TimedAnimation.new(self, current_val, target_val, 350, scroll_target)
                anim.play()
            except Exception:
                self.list_view.scroll_to(position, Gtk.ListScrollFlags.NONE, None)
            return False

        GLib.idle_add(do_scroll)

    def _expand_dir_if_allowed(self, dir_path: Path) -> bool:
        key = str(dir_path)
        if key in self._user_collapsed:
            return False
        pos = self._find_position_for_path(key)
        if pos is None:
            return False
        row = self.tree_model.get_row(pos)
        if row is not None and not row.get_expanded():
            row.set_expanded(True)
        return True

    def _find_position_for_path(self, path_str: str):
        n = self.tree_model.get_n_items()
        for i in range(n):
            row = self.tree_model.get_row(i)
            if row is None:
                continue
            node = row.get_item()
            if str(node.path) == path_str:
                return i
        return None

    def set_root(self, root_path: str):
        self.root_path = Path(root_path)
        self.root_store.remove_all()
        self._node_cache = {}
        self._playing_path = None
        self._folder_cover_cache = {}
        self._track_cover_cache = {}
        self._user_collapsed = set()
        root_node = FileNode(self.root_path, True)
        self.root_store.append(root_node)
        self._node_cache[str(self.root_path)] = root_node
