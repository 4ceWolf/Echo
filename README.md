This project will likely be going on hiatus until I figure out what to use properly due to Gnome 51 blur and GTK4 issues.





# Echo

A native GNOME music player: GTK4 + libadwaita UI, GStreamer playback, full MPRIS2
integration (so GNOME media keys and shell extensions control it
natively), folder-hierarchy file browsing, `.lrc` synced lyrics, and a live spectrum
visualizer.

## Why this exists
Built because every existing player was missing something specific: folder browsing,
lyrics, visualizer, or clean media-key integration, all at once.

## Install (CachyOS / Arch)

```bash
chmod +x install.sh
./install.sh
```

This installs GTK4, libadwaita, GStreamer (+ plugin sets), PyGObject, and mutagen via
pacman, then installs the app to `~/.local/share/echo` with a launcher at
`~/.local/bin/echo-player` and a `.desktop` entry.

Run directly without installing (for dev/testing):
```bash
cd src
python3 main.py
```

## Architecture

- `main.py` — Adw.Application entry point
- `window.py` — main window: sidebar file browser, album art, visualizer, lyrics panel,
  transport controls, volume
- `player.py` — GStreamer `playbin` wrapper. Handles play/pause/seek/volume, queue +
  shuffle/repeat logic, and taps a `spectrum` element in the audio sink chain for
  visualizer data (32-band magnitude array emitted every 50ms)
- `mpris.py` — full `org.mpris.MediaPlayer2` + `.Player` DBus interface implementation
  using `Gio.DBus` directly (no extra dependency, integrates with the GLib main loop
  GTK already runs). This is what your GNOME extension and media keys talk to.
- `filebrowser.py` — `Gtk.TreeListModel`-backed lazy folder tree; expands directories
  on demand, filters to supported audio extensions
- `lyrics.py` — `.lrc` parser (standard `[mm:ss.xx]` tags, tolerant of 1-3 digit
  fractional seconds) with binary-search lookup for the active line at a given
  playback position
- `visualizer.py` — Cairo-drawn bar spectrum in a `Gtk.DrawingArea`, with simple
  peak-hold/decay smoothing so it doesn't look jittery

## How MPRIS hooks into your GNOME extension
Your album-art extension and GNOME's media-key handling both talk to whatever owns
`org.mpris.MediaPlayer2.*` on the session bus. `mpris.py` owns
`org.mpris.MediaPlayer2.Echo` and answers `Metadata` (title/artist/album art
URL), `PlaybackStatus`, `Position`, etc. No extra config needed — it Just Works once
the app is running.

## Lyrics
Drop a `.lrc` file next to the audio file with the same basename
(`song.mp3` + `song.lrc`). Standard format:
```
[00:12.50]First line
[00:15.00]Second line
```

