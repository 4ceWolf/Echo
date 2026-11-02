"""Sidebar tab listing every .m3u playlist found under the library root."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GObject, GdkPixbuf, Gdk

import playlists as playlists_mod


class PlaylistEntry(GObject.Object):
    __gtype_name__ = "PlaylistEntry"

    def __init__(self, playlist: playlists_mod.Playlist):
        super().__init__()
        self.playlist = playlist


class PlaylistBrowser(Gtk.Box):
    __gsignals__ = {
        "track-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "folder-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "folder-tracks-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, int)),
    }

    def __init__(self, root_path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.root_path = root_path

        self.store = Gio.ListStore(item_type=PlaylistEntry)
        self.selection_model = Gtk.SingleSelection(model=self.store)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)

        self.list_view = Gtk.ListView(model=self.selection_model, factory=factory)
        self.list_view.connect("activate", self._on_activate)
        self.list_view.add_css_class("navigation-sidebar")

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self.list_view)
        self.append(scroller)

        self.status_label = Gtk.Label(label="")
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_margin_top(4)
        self.status_label.set_margin_bottom(4)
        self.append(self.status_label)

        self.rescan()

    def rescan(self):
        self.store.remove_all()
        found = playlists_mod.scan_playlists(self.root_path)
        for pl in found:
            self.store.append(PlaylistEntry(pl))
        if not found:
            self.status_label.set_text("No .m3u playlists found")
        else:
            self.status_label.set_text(f"{len(found)} playlists")

    def _on_factory_setup(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        cover = Gtk.Picture()
        cover.set_size_request(40, 40)
        cover.set_content_fit(Gtk.ContentFit.COVER)
        cover_frame = Gtk.Frame()
        cover_frame.set_child(cover)
        box.append(cover_frame)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_label = Gtk.Label(xalign=0)
        title_label.set_ellipsize(3)
        count_label = Gtk.Label(xalign=0)
        count_label.add_css_class("dim-label")
        count_label.add_css_class("caption")
        text_box.append(title_label)
        text_box.append(count_label)
        box.append(text_box)

        list_item.set_child(box)
        list_item._cover = cover
        list_item._title_label = title_label
        list_item._count_label = count_label

    def _on_factory_bind(self, factory, list_item):
        entry: PlaylistEntry = list_item.get_item()
        pl = entry.playlist
        list_item._title_label.set_text(pl.name)
        list_item._count_label.set_text(f"{len(pl.tracks)} tracks")

        texture = None
        if pl.cover_path:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(pl.cover_path, 80, 80, True)
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            except Exception as e:
                print(f"[playlists] failed to load cover {pl.cover_path}: {e}")
        list_item._cover.set_paintable(texture)
        if texture is None:
            list_item._cover.set_paintable(None)

    def _on_activate(self, list_view, position):
        entry: PlaylistEntry = self.selection_model.get_item(position)
        pl = entry.playlist
        if not pl.tracks:
            return
        tracks = list(pl.tracks)
        self.emit("folder-tracks-activated", tracks, 0)
        self.emit("track-activated", tracks[0])

    def set_root(self, root_path: str):
        self.root_path = root_path
        self.rescan()
