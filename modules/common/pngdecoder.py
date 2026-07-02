"""
modules/common/pngdecoder.py — Minimal pure-Python PNG decoder.

Decodes the 8-bit RGB/RGBA, non-interlaced PNGs that screenshot
capture tools (grim, fbgrab) actually produce. Does not attempt to
support the full PNG specification — palette images, 16-bit depth,
and interlacing are explicitly rejected with a clear error rather
than silently mis-decoded, since none of those are things our own
screenshot capture pipeline ever produces.

No external dependencies beyond the standard library (zlib for
DEFLATE decompression, struct for binary parsing). Pillow is not
reliably present on Batocera/Recalbox/RetroPie and this project does
not require anything beyond what these systems ship with — see
CLAUDE.md if you're tempted to add it as a dependency instead of
extending this.
"""

from __future__ import annotations  # Python 3.9 compatibility

import struct
import zlib


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class PNGDecodeError(Exception):
    """
    Raised when a PNG can't be decoded — wrong signature, unsupported
    color type/bit depth/interlacing, or corrupt/truncated data.
    """
    pass


def _paeth_predictor(a: int, b: int, c: int) -> int:
    """The Paeth filter's predictor function, per the PNG spec."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    """
    Reverse PNG's per-scanline filtering.

    `raw` is the decompressed IDAT data: one filter-type byte followed
    by width*bpp data bytes, repeated per scanline. Returns the
    unfiltered pixel data with filter-type bytes stripped out —
    height * width * bpp bytes, scanlines concatenated.

    All arithmetic is mod 256 (PNG filtering operates on raw bytes
    with wraparound, not on signed values) — every addition below is
    masked with & 0xFF for exactly this reason. `a`/`b`/`c` follow the
    spec's own naming: a = byte to the left, b = byte above, c = byte
    above-and-to-the-left, each treated as 0 when out of bounds (first
    column / first row) since there is no real neighbour there.
    """
    stride = width * bpp
    out = bytearray(stride * height)
    prev_row = bytearray(stride)  # zero — stands in for "row above row 0"

    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        row = bytearray(raw[pos:pos + stride])
        pos += stride

        if filter_type == 0:
            pass  # None — row is already correct as-is
        elif filter_type == 1:
            # Sub — each byte reconstructed from the byte bpp positions
            # to its left in THIS (already-reconstructed) row
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + a) & 0xFF
        elif filter_type == 2:
            # Up — each byte reconstructed from the byte directly above
            for x in range(stride):
                row[x] = (row[x] + prev_row[x]) & 0xFF
        elif filter_type == 3:
            # Average of left and above
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev_row[x]
                row[x] = (row[x] + (a + b) // 2) & 0xFF
        elif filter_type == 4:
            # Paeth — the trickiest one to get right; a/b/c are all
            # ALREADY-RECONSTRUCTED values, not raw filtered ones
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev_row[x]
                c = prev_row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + _paeth_predictor(a, b, c)) & 0xFF
        else:
            raise PNGDecodeError(f"Unknown filter type: {filter_type}")

        out[y * stride:(y + 1) * stride] = row
        prev_row = row

    return out


def decode(png_bytes: bytes) -> tuple[int, int, bytearray, int]:
    """
    Decode an 8-bit RGB or RGBA, non-interlaced PNG.

    Args:
        png_bytes: Raw PNG file content.

    Returns:
        (width, height, pixel_data, channels) where pixel_data is a
        flat bytearray of width*height*channels bytes (row-major, no
        row padding), and channels is 3 for RGB or 4 for RGBA.

    Raises:
        PNGDecodeError for anything outside this scope — palette
        images, non-8-bit depth, interlaced images, or corrupt/
        truncated data. Deliberate: a silently-wrong decode of an
        unsupported format would be worse than a clear refusal, since
        a caller doing pixel analysis on garbage data could draw a
        confident, wrong conclusion without any indication something
        was off.
    """
    if png_bytes[:8] != PNG_SIGNATURE:
        raise PNGDecodeError("Not a PNG file (bad signature)")

    pos = 8
    width = height = bit_depth = color_type = None
    idat_chunks = []

    while pos < len(png_bytes):
        if pos + 8 > len(png_bytes):
            raise PNGDecodeError("Truncated PNG (chunk header)")
        length, = struct.unpack('>I', png_bytes[pos:pos + 4])
        chunk_type = png_bytes[pos + 4:pos + 8]
        data_start = pos + 8
        data_end = data_start + length
        if data_end > len(png_bytes):
            raise PNGDecodeError("Truncated PNG (chunk data)")
        data = png_bytes[data_start:data_end]

        if chunk_type == b'IHDR':
            (width, height, bit_depth, color_type,
             compression, filter_method, interlace) = struct.unpack(
                '>IIBBBBB', data
            )
            if compression != 0:
                raise PNGDecodeError("Unsupported compression method")
            if interlace != 0:
                raise PNGDecodeError("Interlaced PNGs are not supported")
            if bit_depth != 8:
                raise PNGDecodeError(
                    f"Only 8-bit depth is supported (got {bit_depth})"
                )
            if color_type not in (2, 6):
                raise PNGDecodeError(
                    f"Only RGB (2) and RGBA (6) color types are "
                    f"supported (got color type {color_type} — "
                    f"palette/grayscale PNGs are not handled)"
                )
        elif chunk_type == b'IDAT':
            idat_chunks.append(data)
        elif chunk_type == b'IEND':
            break

        pos = data_end + 4  # skip the 4-byte CRC, move to next chunk

    if width is None:
        raise PNGDecodeError("No IHDR chunk found")
    if not idat_chunks:
        raise PNGDecodeError("No IDAT chunk found")

    channels = 4 if color_type == 6 else 3
    bpp = channels  # true for 8-bit depth specifically

    compressed = b''.join(idat_chunks)
    try:
        raw = zlib.decompress(compressed)
    except zlib.error as e:
        raise PNGDecodeError(f"zlib decompression failed: {e}")

    expected_len = height * (1 + width * bpp)
    if len(raw) < expected_len:
        raise PNGDecodeError(
            f"Decompressed data shorter than expected: got "
            f"{len(raw)} bytes, expected {expected_len}"
        )

    pixel_data = _unfilter(raw, width, height, bpp)
    return width, height, pixel_data, channels


def get_pixel(
    pixel_data: bytearray, width: int, channels: int, x: int, y: int
) -> tuple:
    """Convenience accessor — returns the (R, G, B) or (R, G, B, A)
    tuple at (x, y). No bounds checking; caller's responsibility."""
    offset = (y * width + x) * channels
    return tuple(pixel_data[offset:offset + channels])
