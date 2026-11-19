"""Artwork and typography for the journal's shared Telegram cover."""

import io
from pathlib import Path

from radar import editorial_sources as sources

PAPER = "#f6f0e5"
INK = "#252b26"
RUST = "#953e2d"
PALETTES = (
    ("#294845", "#faf5e9", "#748d79"),
    ("#e7cba5", "#322d23", "#ac8154"),
    ("#dedfd0", "#273932", "#789386"),
    ("#9f493e", "#fff3e5", "#cf8d75"),
)


def font(size, serif=False):
    from PIL import ImageFont

    names = (
        ("Georgia", "DejaVuSerif", "LiberationSerif-Regular") if serif else
        ("segoeui", "DejaVuSans", "LiberationSans-Regular")
    )
    directories = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"),
                   Path("/usr/share/fonts/truetype/liberation2"))
    for directory in directories:
        for name in names:
            path = directory / f"{name}.ttf"
            if path.exists():
                return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def wrap(draw, text, face, width):
    lines = []
    line = ""
    for word in text.split():
        extended = f"{line} {word}".strip()
        if line and draw.textlength(extended, font=face) > width:
            lines.append(line)
            line = word
        else:
            line = extended
    return [*lines, line]


def title_text(draw, title, width, max_lines, start):
    for size in range(start, 13, -2):
        face = font(size, serif=True)
        lines = wrap(draw, title, face, width)
        if len(lines) <= max_lines and all(draw.textlength(line, font=face) <= width for line in lines):
            break
    return face, "\n".join(lines)


def artwork(asset, bounds, domains):
    from PIL import Image, ImageOps

    if not asset or not sources.images({"image": asset}, {"image_domains": domains}):
        return None
    try:
        body, mime = sources.request(asset["url"], domains, 5_000_001)
        if len(body) > 5_000_000 or not mime.startswith("image/"):
            return None
        with Image.open(io.BytesIO(body)) as image:
            if image.width * image.height > 24_000_000:
                return None
            return ImageOps.contain(ImageOps.exif_transpose(image).convert("RGBA"), bounds)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def tile_image(tile, index, size, domains):
    from PIL import Image, ImageDraw

    background, foreground, accent = PALETTES[index % len(PALETTES)]
    width, height = size
    panel = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(panel)
    margin = 30
    photo = artwork(tile.get("image"), (width - 2 * margin, 136), domains)
    if photo:
        panel.paste(photo, ((width - photo.width) // 2, 47 + (136 - photo.height) // 2), photo)
        title_top, max_lines, start = 194, 2, 34
    else:
        for radius in range(29, 121, 15):
            x, y = width - 37, 28
            draw.arc((x - radius, y - radius, x + radius, y + radius), 35, 215, fill=accent, width=2)
        title_top, max_lines, start = 149, 3, 43
    face, title = title_text(draw, tile["title"], width - 2 * margin, max_lines, start)
    label = tile["section"].upper()
    draw.text((margin, 26), label, fill=foreground, font=font(20), anchor="lt")
    draw.line((margin, title_top - 14, margin + 44, title_top - 14), fill=foreground, width=2)
    draw.multiline_text((margin, title_top), title, fill=foreground, font=face, spacing=3)
    return panel, bool(photo)


def compose(cover):
    from PIL import Image, ImageDraw

    tiles = cover["tiles"]
    if not 1 <= len(tiles) <= 4:
        raise ValueError("Для обложки требуется от одного до четырёх материалов")
    width, margin, gap, tile_height = 1080, 54, 18, 302
    top = 193
    columns = 1 if len(tiles) == 1 else 2
    rows = (len(tiles) + columns - 1) // columns
    height = top + rows * tile_height + (rows - 1) * gap + margin
    canvas = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 38), "Art Radar", fill=INK, font=font(91, serif=True), anchor="lt")
    draw.text((width - margin, 64), cover["date"], fill=INK, font=font(27), anchor="rt")
    draw.line((margin, 150, width - margin, 150), fill=RUST, width=3)
    tile_width = (width - 2 * margin - (columns - 1) * gap) // columns
    domains = sources.read(sources.ROOT / "sources.json")["image_domains"]
    originals = 0
    for index, tile in enumerate(tiles):
        x = margin + index % columns * (tile_width + gap)
        y = top + index // columns * (tile_height + gap)
        size = (tile_width, tile_height)
        if index == len(tiles) - 1 and index % columns == 0:
            size = (width - 2 * margin, tile_height)
        panel, original = tile_image(tile, index, size, domains)
        canvas.paste(panel, (x, y))
        originals += original
    output = io.BytesIO()
    canvas.save(output, "JPEG", quality=92, optimize=True)
    return output.getvalue(), originals
