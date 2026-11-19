"""Artwork and typography for the journal's shared Telegram cover."""

import hashlib
import io
from pathlib import Path

from radar import editorial_sources as sources

PAPER = "#f6f0e5"
INK = "#252b26"
RUST = "#953e2d"
PHOTO_MARGIN = 10
PHOTO_GAP = 7
PHOTO_TOP = 66
COLLECTION_LICENSE = "creativecommons.org/licenses/by-sa/4.0"
COVER_AGENT = "ArtRadarCover/1.0 (https://github.com/YuriyKhudov/art-radar)"
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


def artwork(asset, domains):
    from PIL import Image, ImageOps

    if not asset:
        return None
    if asset.get("license", "").casefold() == "public domain":
        asset = {**asset, "license": "Public Domain"}
    licensed = sources.images({"image": asset}, {"image_domains": domains})
    if asset.get("license") == "CC BY-SA 4.0":
        origin = asset.get("source", "")
        admitted = sources.allowed(origin, ["commons.wikimedia.org"]) or (
            asset.get("license_url", "").rstrip("/") == f"https://{COLLECTION_LICENSE}" and sources.allowed(origin, domains))
        licensed = (asset.get("credit") and sources.allowed(asset.get("url", ""), domains)
                    and admitted)
    if not licensed:
        return None
    try:
        body, mime = sources.request(asset["url"], domains, 5_000_001, user_agent=COVER_AGENT)
        if len(body) > 5_000_000 or not mime.startswith("image/"):
            return None
        digest = hashlib.sha256(body).hexdigest()
        if asset.get("sha256") and asset["sha256"] != digest:
            return None
        with Image.open(io.BytesIO(body)) as image:
            if image.width * image.height > 24_000_000 or not 0.125 <= image.width / image.height <= 8:
                return None
            original = ImageOps.exif_transpose(image).convert("RGBA")
            original.info["art_radar_sha256"] = digest
            return original
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def tile_image(tile, index, size):
    from PIL import Image, ImageDraw

    background, foreground, accent = PALETTES[index % len(PALETTES)]
    width, height = size
    panel = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(panel)
    margin = 30
    for radius in range(29, 121, 15):
        x, y = width - 37, 28
        draw.arc((x - radius, y - radius, x + radius, y + radius), 35, 215, fill=accent, width=2)
    title_top = 149
    face, title = title_text(draw, tile["title"], width - 2 * margin, 3, 43)
    label = tile["section"].upper()
    draw.text((margin, 26), label, fill=foreground, font=font(20), anchor="lt")
    draw.line((margin, title_top - 14, margin + 44, title_top - 14), fill=foreground, width=2)
    draw.multiline_text((margin, title_top), title, fill=foreground, font=face, spacing=3)
    return panel


def text_cover(cover):
    from PIL import Image, ImageDraw

    tiles = cover["tiles"]
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
    for index, tile in enumerate(tiles):
        x = margin + index % columns * (tile_width + gap)
        y = top + index // columns * (tile_height + gap)
        size = (tile_width, tile_height)
        if index == len(tiles) - 1 and index % columns == 0:
            size = (width - 2 * margin, tile_height)
        canvas.paste(tile_image(tile, index, size), (x, y))
    return canvas


def photo_layout(ratios, width):
    """Use complete images, choosing a spread or a stack to suit the available shapes."""
    gap = PHOTO_GAP
    if len(ratios) == 3 and ratios[0] < 1:
        lead, second, third = ratios
        right = max(1, round((width - gap - lead * gap) / (1 + lead * (1 / second + 1 / third))))
        left = width - gap - right
        upper, lower = round(right / second), round(right / third)
        return [(0, 0, left, upper + gap + lower),
                (left + gap, 0, right, upper), (left + gap, upper + gap, right, lower)]

    rows = [ratios]
    if len(ratios) == 2 and sum(ratios) > 2.7:
        rows = [[ratios[0]], [ratios[1]]]
    elif len(ratios) == 3:
        rows = [ratios[:1], ratios[1:]]
    elif len(ratios) == 4:
        rows = [ratios[:2], ratios[2:]]
    boxes, y = [], 0
    for row in rows:
        available = width - gap * (len(row) - 1)
        scale = available / sum(row)
        height = max(1, round(scale))
        x = 0
        for position, ratio in enumerate(row):
            tile_width = width - x if position == len(row) - 1 else max(1, round(ratio * scale))
            boxes.append((x, y, tile_width, height))
            x += tile_width + gap
        y += height + gap
    return boxes


def photo_cover(date, originals):
    from PIL import Image, ImageDraw, ImageOps

    ratios = [image.width / image.height for image, _ in originals]
    content_width = 1080 - 2 * PHOTO_MARGIN
    boxes = photo_layout(ratios, content_width)
    photo_height = max(y + height for _, y, _, height in boxes)
    if photo_height > 1400:
        content_width = max(280, round(content_width * 1400 / photo_height))
        boxes = photo_layout(ratios, content_width)
        photo_height = max(y + height for _, y, _, height in boxes)

    width = content_width + 2 * PHOTO_MARGIN
    credits = []
    for _, asset in originals:
        source = asset.get("source", "").removeprefix("https://")
        credits.append(" · ".join(filter(None, (asset["credit"], source))))
    has_share_alike = any(asset.get("license") == "CC BY-SA 4.0" for _, asset in originals)
    columns = 2 if width >= 760 else 1
    footer_rows = (len(credits) + columns - 1) // columns
    footer_top = PHOTO_TOP + photo_height + 6
    canvas = Image.new("RGB", (width, footer_top + (footer_rows + has_share_alike) * 17 + 5), PAPER)
    draw = ImageDraw.Draw(canvas)
    masthead = font(min(51, width // 10), serif=True)
    draw.text((PHOTO_MARGIN, 7), "Art Radar", fill=INK, font=masthead, anchor="lt")
    draw.text((width - PHOTO_MARGIN, 26), date, fill=INK, font=font(min(23, width // 22)), anchor="rt")
    draw.line((PHOTO_MARGIN, 61, width - PHOTO_MARGIN, 61), fill=RUST, width=2)
    for (image, _), (x, y, frame_width, frame_height) in zip(originals, boxes):
        fitted = ImageOps.contain(image, (frame_width, frame_height))
        position = (PHOTO_MARGIN + x + (frame_width - fitted.width) // 2,
                    PHOTO_TOP + y + (frame_height - fitted.height) // 2)
        canvas.paste(fitted, position, fitted)
    column_width = content_width // columns
    for index, credit in enumerate(credits):
        face = font(15)
        while draw.textlength(credit, font=face) > column_width - 10 and face.size > 9:
            face = font(face.size - 1)
        draw.text((PHOTO_MARGIN + index % columns * column_width, footer_top + index // columns * 17),
                  credit, fill=INK, font=face, anchor="lt")
    if has_share_alike:
        draw.text((PHOTO_MARGIN, footer_top + footer_rows * 17), f"CC BY-SA 4.0 · {COLLECTION_LICENSE}",
                  fill=INK, font=font(14), anchor="lt")
    return canvas


def compose(cover):
    tiles = cover["tiles"]
    if not 1 <= len(tiles) <= 4:
        raise ValueError("Для обложки требуется от одного до четырёх материалов")
    domains = sources.read(sources.ROOT / "sources.json")["image_domains"]
    originals, used = [], set()
    for tile in tiles:
        asset = tile.get("image")
        if not asset or not sources.allowed(asset.get("url", ""), domains):
            continue
        key = sources.canonical(asset["url"])
        if key in used:
            continue
        image = artwork(asset, domains)
        if image:
            originals.append((image, asset))
            used.add(key)
    canvas = photo_cover(cover["date"], originals) if originals else text_cover(cover)
    output = io.BytesIO()
    canvas.save(output, "JPEG", quality=92, optimize=True)
    return output.getvalue(), len(originals)
