# Maintainer: 4ceWolf <4ceWolf@users.noreply.github.com>
pkgname=echo-player
pkgver=1.0.0
pkgrel=1
pkgdesc="A GTK4/libadwaita music player with visualizer, lyrics, waveform, and smart shuffle"
arch=('x86_64' 'aarch64')
url="https://github.com/4ceWolf/Echo"
license=('GPL3')
depends=(
    'python'
    'python-gobject'
    'gtk4'
    'libadwaita'
    'gstreamer'
    'gst-plugins-base'
    'gst-plugins-good'
    'gst-plugins-bad'
    'gst-plugins-ugly'
    'gst-libav'
    'python-mutagen'
    'python-pillow'
    'python-cairo'
)
optdepends=(
    'cava: live audio visualizer bars (falls back to GStreamer spectrum without it)'
)
source=("$pkgname-$pkgver.tar.gz")
sha256sums=('SKIP')

# For local development / AUR, you can also point source at a git repo:
# source=("git+https://github.com/yourusername/echo-player.git")
# sha256sums=('SKIP')

_installdir="/usr/lib/$pkgname"

package() {
    cd "$srcdir"

    # App source files
    install -dm755 "$pkgdir/$_installdir"
    install -dm755 "$pkgdir/$_installdir/assets"
    cp -r src/* "$pkgdir/$_installdir/"

    # Assets
    install -Dm644 "src/assets/echo-logo.png" \
        "$pkgdir/$_installdir/assets/echo-logo.png"

    # Desktop entry
    install -Dm644 "data/io.echo.Player.desktop" \
        "$pkgdir/usr/share/applications/io.echo.Player.desktop"

    # App icon (multiple sizes via hicolor)
    install -Dm644 "data/io.echo.Player.png" \
        "$pkgdir/usr/share/icons/hicolor/256x256/apps/io.echo.Player.png"
    install -Dm644 "src/assets/echo-logo.png" \
        "$pkgdir/usr/share/icons/hicolor/256x256/apps/echo-player.png"

    # Launcher script
    install -dm755 "$pkgdir/usr/bin"
    cat > "$pkgdir/usr/bin/echo-player" << LAUNCHER
#!/usr/bin/env bash
exec python3 "$_installdir/main.py" "\$@"
LAUNCHER
    chmod 755 "$pkgdir/usr/bin/echo-player"

    # GNOME Shell blur extension (optional, Wayland/GNOME only)
    local ext_uuid="echo-blur@local"
    if [ -d "shell-extension/$ext_uuid" ]; then
        install -dm755 "$pkgdir/usr/share/gnome-shell/extensions/$ext_uuid"
        cp -r "shell-extension/$ext_uuid/"* \
            "$pkgdir/usr/share/gnome-shell/extensions/$ext_uuid/"
    fi
}

post_install() {
    gtk-update-icon-cache -q -t -f usr/share/icons/hicolor 2>/dev/null || true
    echo "Launch with: echo-player"
    echo "Or open 'Echo' from your app grid."
    echo ""
    echo "Optional: install 'cava' from AUR/extra for a live audio visualizer:"
    echo "  yay -S cava"
}

post_upgrade() {
    post_install
}
