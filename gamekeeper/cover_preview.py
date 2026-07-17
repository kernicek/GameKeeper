"""Server-side rendering of the square grid-tile cover preview (issue #104).

The collection grid used to load the full-size ``cover_image`` and crop it
in the browser (``object-fit: cover`` + ``object-position`` + a ``scale()``
transform, or ``object-fit: contain`` over a letterbox backdrop for
zoom-out "fit" tiles). That pulled a page of large images down for the grid.
This module bakes that exact tile crop into a small square PNG so the grid
can serve a lightweight derivative instead.

``render_square_preview`` reproduces the tile geometry algebraically, so a
baked preview looks the same as the old CSS-cropped tile:

* Cover mode (``zoom >= 100``): the tile crops to a square of side
  ``min(w, h) / zoom`` positioned by the focus point. That is the closed
  form of ``object-fit: cover`` + ``object-position`` + ``scale()`` about
  the focus origin — the visible window is a focus-positioned square.
* Fit mode (``zoom < 100``): the whole art shows contain-style over the
  chosen letterbox colour, scaled by the same aspect-aware factor the CSS
  uses (``CoverArtModel.cover_scale``). No colour => transparent letterbox
  so the tile background shows through, matching the old "no backdrop"
  rendering.
"""

import io

from PIL import Image

# 2x the 160px grid-tile minimum (`.cover-grid` uses minmax(160px, 1fr)),
# so the preview stays sharp at 2x DPR on a min-width tile. Bump this if the
# stretched tiles (up to ~320 CSS px) need to be crisp at retina too.
PREVIEW_SIZE = 320


def _fit_background(fit_color):
    """RGBA fill for the fit-mode letterbox: the opaque chosen colour, or a
    transparent canvas when unset so the tile background shows through."""
    color = (fit_color or "").lstrip("#")
    if len(color) == 6:
        try:
            r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
            return (r, g, b, 255)
        except ValueError:
            pass
    return (0, 0, 0, 0)


def render_square_preview(data, focus_x, focus_y, zoom, fit_color,
                          size=PREVIEW_SIZE):
    """Bake the square grid-tile crop of the cover bytes ``data`` into a
    ``size`` x ``size`` PNG, returning the PNG bytes.

    ``focus_x``/``focus_y`` are 0-100 percentages, ``zoom`` a 50-300 percent,
    ``fit_color`` an "#rrggbb" string or "" — the same fields the cover
    editor writes.
    """
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGBA")
        width, height = image.size
        fx, fy = focus_x / 100, focus_y / 100

        if zoom >= 100:
            # Cover crop: a focus-positioned square of the source, resized to
            # fill the tile. box= resamples straight from the float region.
            side = min(width, height) / (zoom / 100)
            left = fx * (width - side)
            top = fy * (height - side)
            out = image.resize(
                (size, size), Image.LANCZOS,
                box=(left, top, left + side, top + side))
        else:
            # Fit / letterbox: contain the whole art, scaled by the same
            # aspect-aware factor as CoverArtModel.cover_scale, over the
            # chosen colour (or transparent). Overflow / negative offsets
            # clip against the canvas just like the CSS tile.
            k = min(size / width, size / height)
            ratio = max(width, height) / min(width, height)
            scale = 1 + (ratio - 1) * (zoom - 50) / 50
            art_w = max(1, round(scale * width * k))
            art_h = max(1, round(scale * height * k))
            art = image.resize((art_w, art_h), Image.LANCZOS)
            out = Image.new("RGBA", (size, size), _fit_background(fit_color))
            paste_x = round(fx * (size - scale * width * k))
            paste_y = round(fy * (size - scale * height * k))
            out.paste(art, (paste_x, paste_y), art)

    buffer = io.BytesIO()
    out.save(buffer, format="PNG")
    return buffer.getvalue()
