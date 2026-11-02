"""Shared album-art helpers: extraction from tags, disk caching for MPRIS,
real Gaussian blur for the frosted background, and 2x2 collage covers for
playlists/folders that have no folder.jpg/png of their own."""
import hashlib
import io as _io
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GdkPixbuf, Gdk, GLib

try:
    import cairo
except ImportError:
    cairo = None

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

try:
    from PIL import Image, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

ART_CACHE_DIR = Path.home() / ".cache" / "echo" / "art"
ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
COLLAGE_CACHE_DIR = Path.home() / ".cache" / "echo" / "collages"
COLLAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

COVER_NAMES = [
    "folder.jpg", "folder.png", "folder.jpeg",
    "Folder.jpg", "Folder.png",
    "cover.jpg", "cover.png", "cover.jpeg",
    "Cover.jpg", "Cover.png",
    "album.jpg", "album.png",
]


def find_folder_cover_file(dir_path: Path):
    for name in COVER_NAMES:
        candidate = dir_path / name
        if candidate.exists():
            return candidate
    return None


def resolve_folder_cover(dir_path: Path, cache_key: str = None):
    """folder.jpg/png (checked first) else a 2x2 collage from the first four
    audio tracks directly inside dir_path. Used by both the folder browser
    and playlist covers so the same rule applies everywhere."""
    from config import SUPPORTED_EXTENSIONS
    cover = find_folder_cover_file(dir_path)
    if cover is not None:
        return str(cover)
    try:
        tracks = sorted(
            [str(p) for p in dir_path.iterdir()
             if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS],
            key=str.lower,
        )
    except (PermissionError, FileNotFoundError):
        return None
    if not tracks:
        return None
    return build_collage_cover(tracks, cache_key=cache_key or str(dir_path))


def extract_embedded_art(filepath: str):
    if MutagenFile is None:
        return None
    try:
        audio = MutagenFile(filepath)
        if audio is None or not audio.tags:
            return None
        tags = audio.tags
        try:
            for tag in tags.values():
                if tag.__class__.__name__ == "APIC":
                    return tag.data
        except Exception:
            pass
        try:
            if hasattr(audio, "pictures") and audio.pictures:
                return audio.pictures[0].data
        except Exception:
            pass
        try:
            if "covr" in audio:
                return bytes(audio["covr"][0])
        except Exception:
            pass
    except Exception:
        pass
    return None


def sniff_image_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".jpg"


def cache_art_bytes(key: str, data: bytes):
    try:
        ext = sniff_image_ext(data)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        out_path = ART_CACHE_DIR / f"{h}{ext}"
        out_path.write_bytes(data)
        return out_path.as_uri()
    except Exception as e:
        print(f"[art] failed to cache art for {key}: {e}")
        return None


def pixbuf_from_bytes(data: bytes):
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()
    except Exception as e:
        print(f"[art] failed to decode image data: {e}")
        return None


def build_collage_cover(track_paths: list, cache_key: str, size: int = 300):
    """2x2 collage from up to the first 4 tracks' embedded art. Returns a
    cached file path, or None if none of the first four tracks had art."""
    if cairo is None:
        return None
    arts = []
    for p in track_paths[:4]:
        data = extract_embedded_art(p)
        if data:
            pixbuf = pixbuf_from_bytes(data)
            if pixbuf is not None:
                arts.append(pixbuf)
    if not arts:
        return None

    h = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
    out_path = COLLAGE_CACHE_DIR / f"{h}.png"
    if out_path.exists():
        return str(out_path)

    half = size // 2
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    positions = [(0, 0), (half, 0), (0, half), (half, half)]
    for i, (x, y) in enumerate(positions):
        pixbuf = arts[i % len(arts)]
        scaled = pixbuf.scale_simple(half, half, GdkPixbuf.InterpType.BILINEAR)
        Gdk.cairo_set_source_pixbuf(ctx, scaled, x, y)
        ctx.paint()
    surface.write_to_png(str(out_path))
    return str(out_path)


def make_blurred_pixbuf(pixbuf: GdkPixbuf.Pixbuf, tint_rgb=None, tint_alpha: float = 0.35,
                         target_size: int = 480, blur_radius: int = 30):
    """Genuinely blurred (Gaussian via Pillow if available), optionally
    color-tinted version of an album art pixbuf, for use as a full-window
    background. Falls back to a multi-pass box blur if Pillow isn't
    installed (still much smoother than a single low-res stretch)."""
    if HAS_PIL:
        try:
            success, buf = pixbuf.save_to_bufferv("png", [], [])
            pil_img = Image.open(_io.BytesIO(bytes(buf))).convert("RGB")
            pil_img = pil_img.resize((target_size, target_size), Image.BILINEAR)
            pil_img = pil_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
            if tint_rgb is not None:
                r, g, b = [int(c * 255) for c in tint_rgb]
                tint_layer = Image.new("RGB", pil_img.size, (r, g, b))
                pil_img = Image.blend(pil_img, tint_layer, tint_alpha)
            out_buf = _io.BytesIO()
            pil_img.save(out_buf, format="PNG")
            return pixbuf_from_bytes(out_buf.getvalue())
        except Exception as e:
            print(f"[art] Pillow blur failed, falling back to box blur: {e}")

    small = pixbuf
    for tile in (96, 48, 24, 12):
        small = small.scale_simple(tile, tile, GdkPixbuf.InterpType.BILINEAR)
    result = small.scale_simple(target_size, target_size, GdkPixbuf.InterpType.BILINEAR)
    if tint_rgb is not None:
        result = _tint_pixbuf(result, tint_rgb, tint_alpha)
    return result


def _tint_pixbuf(pixbuf: GdkPixbuf.Pixbuf, tint_rgb, alpha: float) -> GdkPixbuf.Pixbuf:
    r_t, g_t, b_t = [int(c * 255) for c in tint_rgb]
    n_channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    w, h = pixbuf.get_width(), pixbuf.get_height()
    buf = bytearray(pixbuf.get_pixels())
    for y in range(h):
        row_start = y * stride
        for x in range(w):
            off = row_start + x * n_channels
            buf[off] = int(buf[off] * (1 - alpha) + r_t * alpha)
            buf[off + 1] = int(buf[off + 1] * (1 - alpha) + g_t * alpha)
            buf[off + 2] = int(buf[off + 2] * (1 - alpha) + b_t * alpha)
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(buf)), GdkPixbuf.Colorspace.RGB,
        pixbuf.get_has_alpha(), 8, w, h, stride,
    )
