#!/usr/bin/env python3
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

from config import APP_ID
from window import EchoWindow


class EchoApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = EchoWindow(application=self)
        self.window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        paths = [f.get_path() for f in files if f.get_path()]
        if paths and self.window:
            self.window.player.set_queue(paths, 0)
            self.window.player.load_and_play(paths[0])
            self.window.queue_view.refresh(self.window.player.queue, self.window.player.queue_index)


def main():
    app = EchoApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
