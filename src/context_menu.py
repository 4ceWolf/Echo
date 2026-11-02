"""Right-click context menus for track rows and folder rows."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Gdk, GLib
from pathlib import Path
from config import SUPPORTED_EXTENSIONS


def show_in_file_manager(filepath: str):
    try:
        p = Path(filepath)
        folder = p.parent if p.is_file() else p
        uri = folder.as_uri()
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except Exception as e:
        print(f"[context_menu] failed to open folder: {e}")


def attach_track_context_menu(widget: Gtk.Widget, get_filepath_at, on_play_next, on_add_to_end):
    gesture = Gtk.GestureClick(button=3)

    def on_pressed(gesture_, n_press, x, y):
        filepath = get_filepath_at()
        if not filepath:
            return
        menu = Gio.Menu()
        menu.append("Play Next", "track-menu.play-next")
        menu.append("Add to End of Queue", "track-menu.add-to-end")
        menu.append("Show in Files", "track-menu.show-in-files")

        action_group = Gio.SimpleActionGroup()
        play_next_action = Gio.SimpleAction.new("play-next", None)
        play_next_action.connect("activate", lambda a, p: on_play_next(filepath))
        add_end_action = Gio.SimpleAction.new("add-to-end", None)
        add_end_action.connect("activate", lambda a, p: on_add_to_end(filepath))
        show_action = Gio.SimpleAction.new("show-in-files", None)
        show_action.connect("activate", lambda a, p: show_in_file_manager(filepath))
        action_group.add_action(play_next_action)
        action_group.add_action(add_end_action)
        action_group.add_action(show_action)
        widget.insert_action_group("track-menu", action_group)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.set_has_arrow(True)
        popover.popup()

    gesture.connect("pressed", on_pressed)
    widget.add_controller(gesture)


def attach_folder_context_menu(widget: Gtk.Widget, get_folderpath_at, on_play_next, on_add_to_end):
    """Right-click context menu for folder rows: queue all tracks recursively."""
    gesture = Gtk.GestureClick(button=3)

    def collect_tracks(folder: Path):
        tracks = []
        try:
            for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if entry.is_dir():
                    tracks.extend(collect_tracks(entry))
                elif entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                    tracks.append(str(entry))
        except (PermissionError, FileNotFoundError):
            pass
        return tracks

    def on_pressed(gesture_, n_press, x, y):
        folderpath = get_folderpath_at()
        if not folderpath:
            return
        menu = Gio.Menu()
        menu.append("Play Folder Next", "folder-menu.play-next")
        menu.append("Add Folder to End of Queue", "folder-menu.add-to-end")

        action_group = Gio.SimpleActionGroup()
        play_next_action = Gio.SimpleAction.new("play-next", None)
        def do_play_next(a, p, fp=folderpath):
            tracks = collect_tracks(Path(fp))
            for t in reversed(tracks):
                on_play_next(t)
        play_next_action.connect("activate", do_play_next)

        add_end_action = Gio.SimpleAction.new("add-to-end", None)
        def do_add_end(a, p, fp=folderpath):
            tracks = collect_tracks(Path(fp))
            for t in tracks:
                on_add_to_end(t)
        add_end_action.connect("activate", do_add_end)

        action_group.add_action(play_next_action)
        action_group.add_action(add_end_action)
        widget.insert_action_group("folder-menu", action_group)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.set_has_arrow(True)
        popover.popup()

    gesture.connect("pressed", on_pressed)
    widget.add_controller(gesture)
