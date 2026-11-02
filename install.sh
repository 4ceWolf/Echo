#!/usr/bin/env bash
set -e

echo "Stopping any running Echo instance..."
pkill -f echo-player 2>/dev/null || true

echo "Installing Echo dependencies (pacman)..."
sudo pacman -S --needed --noconfirm \
    python \
    python-gobject \
    gtk4 \
    libadwaita \
    gstreamer \
    gst-plugins-base \
    gst-plugins-good \
    gst-plugins-bad \
    gst-plugins-ugly \
    gst-libav \
    python-mutagen \
    python-pillow \
    python-cairo \
    cava

INSTALL_DIR="$HOME/.local/share/echo"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "Removing any previous Echo install so old cached files can't linger..."
rm -rf "$INSTALL_DIR"

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR/src/"* "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/assets"
cp "$SCRIPT_DIR/data/io.echo.Player.png" "$INSTALL_DIR/assets/echo-logo.png" 2>/dev/null || true

cat > "$BIN_DIR/echo-player" << EOF
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/echo-player"

cp "$SCRIPT_DIR/data/io.echo.Player.desktop" "$DESKTOP_DIR/"

# Install the app icon into the user icon theme so the app grid shows the real logo
ICON_DEST_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$ICON_DEST_DIR"
cp "$SCRIPT_DIR/data/io.echo.Player.png" "$ICON_DEST_DIR/io.echo.Player.png"
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# GNOME Shell extension that applies real blur-behind-window to Echo
# specifically (see shell-extension/echo-blur@local/extension.js for
# how/why). Only makes sense on GNOME/Mutter, so this is best-effort.
EXT_UUID="echo-blur@local"
EXT_DEST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
if [ -d "$SCRIPT_DIR/shell-extension/$EXT_UUID" ]; then
    echo "Installing the Echo Blur GNOME Shell extension..."
    mkdir -p "$HOME/.local/share/gnome-shell/extensions"
    rm -rf "$EXT_DEST"
    cp -r "$SCRIPT_DIR/shell-extension/$EXT_UUID" "$EXT_DEST"
    if command -v gnome-extensions >/dev/null 2>&1; then
        gnome-extensions enable "$EXT_UUID" 2>/dev/null || \
            echo "Couldn't auto-enable it - open the Extensions app and enable 'Echo Blur' manually."
    else
        echo "gnome-extensions CLI not found - open the Extensions app and enable 'Echo Blur' manually."
    fi
    echo "Note: on Wayland, GNOME Shell extensions often need a full logout/login (not just app restart) to take effect."
fi

echo ""
echo "Done. Make sure ~/.local/bin is in your PATH (fish: 'set -Ux fish_user_paths \$HOME/.local/bin \$fish_user_paths')."
echo "Launch with: echo-player"
echo "Or find 'Echo' in your GNOME app grid (may need a logout/relogin for the desktop entry to show up)."
