"""
MPRIS2 (Media Player Remote Interfacing Specification) implementation.
This is what lets GNOME media keys, the Hanabi-style GNOME extensions,
and shell media controls see and control the player.
"""
from gi.repository import Gio, GLib
from player import REPEAT_OFF, REPEAT_ONE, REPEAT_ALL

MPRIS_IFACE_ROOT = "org.mpris.MediaPlayer2"
MPRIS_IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
MPRIS_PATH = "/org/mpris/MediaPlayer2"

INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
  </interface>
</node>
"""


class MprisService:
    """Registers org.mpris.MediaPlayer2.Echo on the session bus."""

    def __init__(self, app_name: str, player, get_current_track_info):
        """
        player: instance of Player (from player.py)
        get_current_track_info: callable -> dict with keys title/artist/album/art_url/length_us/path
        """
        self.app_name = app_name
        self.player = player
        self.get_current_track_info = get_current_track_info
        self.node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self.bus = None
        self.reg_id_root = None
        self.reg_id_player = None
        self.owner_id = None

        player.connect("state-changed", self._on_state_changed)
        player.connect("settings-changed", lambda p: self.notify_properties_changed())

    def start(self):
        self.owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            f"org.mpris.MediaPlayer2.{self.app_name}",
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            None,
            None,
        )

    def _on_bus_acquired(self, connection: Gio.DBusConnection, name: str):
        self.bus = connection
        root_iface = self.node_info.lookup_interface(MPRIS_IFACE_ROOT)
        player_iface = self.node_info.lookup_interface(MPRIS_IFACE_PLAYER)

        self.reg_id_root = connection.register_object(
            MPRIS_PATH, root_iface, self._handle_root_call, self._get_root_prop, None
        )
        self.reg_id_player = connection.register_object(
            MPRIS_PATH, player_iface, self._handle_player_call, self._get_player_prop,
            self._set_player_prop,
        )

    # --- Root interface (org.mpris.MediaPlayer2) ---
    def _handle_root_call(self, connection, sender, path, interface, method, params, invocation):
        if method == "Raise":
            invocation.return_value(None)
        elif method == "Quit":
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    def _get_root_prop(self, connection, sender, path, interface, prop_name):
        if prop_name == "CanQuit":
            return GLib.Variant("b", False)
        if prop_name == "CanRaise":
            return GLib.Variant("b", True)
        if prop_name == "HasTrackList":
            return GLib.Variant("b", False)
        if prop_name == "Identity":
            return GLib.Variant("s", "Echo")
        if prop_name == "DesktopEntry":
            return GLib.Variant("s", "io.echo.Player")
        if prop_name == "SupportedUriSchemes":
            return GLib.Variant("as", ["file"])
        if prop_name == "SupportedMimeTypes":
            return GLib.Variant("as", ["audio/mpeg", "audio/flac", "audio/ogg", "audio/x-wav"])
        return None

    # --- Player interface ---
    def _handle_player_call(self, connection, sender, path, interface, method, params, invocation):
        if method == "Next":
            self.player.next_track()
        elif method == "Previous":
            self.player.prev_track()
        elif method == "Pause":
            self.player.pause()
        elif method == "PlayPause":
            self.player.toggle_play_pause()
        elif method == "Stop":
            self.player.stop()
        elif method == "Play":
            self.player.play()
        elif method == "Seek":
            offset_us = params.unpack()[0]
            new_pos_ms = self.player.get_position_ms() + offset_us // 1000
            self.player.seek(max(0, new_pos_ms))
        elif method == "SetPosition":
            _track_id, pos_us = params.unpack()
            self.player.seek(pos_us // 1000)
        elif method == "OpenUri":
            pass
        invocation.return_value(None)

    def _get_player_prop(self, connection, sender, path, interface, prop_name):
        if prop_name == "PlaybackStatus":
            status = "Playing" if self.player.is_playing() else "Paused"
            if not self.player.current_uri:
                status = "Stopped"
            return GLib.Variant("s", status)
        if prop_name == "LoopStatus":
            mapping = {"off": "None", "one": "Track", "all": "Playlist"}
            return GLib.Variant("s", mapping.get(self.player.repeat, "None"))
        if prop_name == "Rate":
            return GLib.Variant("d", 1.0)
        if prop_name == "Shuffle":
            return GLib.Variant("b", self.player.shuffle)
        if prop_name == "Metadata":
            return GLib.Variant("a{sv}", self._build_metadata())
        if prop_name == "Volume":
            return GLib.Variant("d", self.player.pipeline.get_property("volume"))
        if prop_name == "Position":
            return GLib.Variant("x", self.player.get_position_ms() * 1000)
        if prop_name == "MinimumRate":
            return GLib.Variant("d", 1.0)
        if prop_name == "MaximumRate":
            return GLib.Variant("d", 1.0)
        if prop_name == "CanGoNext":
            return GLib.Variant("b", True)
        if prop_name == "CanGoPrevious":
            return GLib.Variant("b", True)
        if prop_name == "CanPlay":
            return GLib.Variant("b", True)
        if prop_name == "CanPause":
            return GLib.Variant("b", True)
        if prop_name == "CanSeek":
            return GLib.Variant("b", True)
        if prop_name == "CanControl":
            return GLib.Variant("b", True)
        return None

    def _set_player_prop(self, connection, sender, path, interface, prop_name, value):
        """Handle writes from MPRIS clients (media keys, GNOME extensions like Hanabi)."""
        if prop_name == "LoopStatus":
            mapping = {"None": REPEAT_OFF, "Track": REPEAT_ONE, "Playlist": REPEAT_ALL}
            self.player.set_repeat(mapping.get(value.get_string(), REPEAT_OFF))
            return True
        if prop_name == "Shuffle":
            self.player.set_shuffle(value.get_boolean())
            return True
        if prop_name == "Volume":
            self.player.set_volume(value.get_double())
            return True
        if prop_name == "Rate":
            return True  # we don't support variable playback rate; accept and ignore
        return False

    def _build_metadata(self) -> dict:
        info = self.get_current_track_info() or {}
        track_id = "/io/echo/Player/CurrentTrack"
        meta = {
            "mpris:trackid": GLib.Variant("o", track_id),
            "xesam:title": GLib.Variant("s", info.get("title", "Unknown")),
            "xesam:artist": GLib.Variant("as", [info.get("artist", "Unknown Artist")]),
            "xesam:album": GLib.Variant("s", info.get("album", "")),
        }
        if info.get("length_us"):
            meta["mpris:length"] = GLib.Variant("x", info["length_us"])
        if info.get("art_url"):
            meta["mpris:artUrl"] = GLib.Variant("s", info["art_url"])
        return meta

    # --- Notify shell of changes ---
    def _on_state_changed(self, player, state):
        self.notify_properties_changed()

    def notify_properties_changed(self):
        if not self.bus:
            return
        changed = {
            "PlaybackStatus": self._get_player_prop(None, None, None, None, "PlaybackStatus"),
            "Metadata": self._get_player_prop(None, None, None, None, "Metadata"),
            "Shuffle": self._get_player_prop(None, None, None, None, "Shuffle"),
            "LoopStatus": self._get_player_prop(None, None, None, None, "LoopStatus"),
        }
        variant = GLib.Variant(
            "(sa{sv}as)",
            (MPRIS_IFACE_PLAYER, changed, []),
        )
        self.bus.emit_signal(
            None,
            MPRIS_PATH,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            variant,
        )

    def notify_seeked(self, position_ms: int):
        if not self.bus:
            return
        self.bus.emit_signal(
            None, MPRIS_PATH, MPRIS_IFACE_PLAYER, "Seeked",
            GLib.Variant("(x)", (position_ms * 1000,)),
        )
