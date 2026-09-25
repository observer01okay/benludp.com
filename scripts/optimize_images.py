#!/usr/bin/env python3
"""Visually-lossless PNG -> JPEG for the built site.

Originals in assets/ are never touched; only dist/ gets the smaller files.
Each image gets the *lowest* JPEG quality that still measures as visually
identical to the original (PSNR >= MIN_PSNR, full 4:4:4 color, no chroma
subsampling). Grainy frames therefore keep high quality; clean ones shrink
more. If no setting saves enough, the PNG is kept as-is.

Results are cached in .cache/images/ (keyed by the PNG's content), so only
new or changed images are re-encoded on later builds.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "images"

# Folders holding portfolio photos (favicons / brand marks are left alone).
PHOTO_DIRS = ("work", "projects", "stills", "info")
MIN_PSNR = 46.0  # dB; ~45+ is generally indistinguishable to the eye
QUALITIES = range(90, 101)
MAX_RATIO = 0.8  # keep the PNG unless the JPEG is at least 20% smaller
VERSION = "v1"  # bump to force re-encoding everything


def _tools_ok() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("cjpeg"))


def _has_alpha(png: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(png)],
        capture_output=True, text=True,
    ).stdout
    return "a" in out.replace("gray", "")  # rgba, ya8, pal8 w/ alpha, etc.


def _psnr(ref: Path, test: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-i", str(ref), "-i", str(test), "-lavfi",
         "[0:v]format=rgb24[a];[1:v]format=rgb24[b];[a][b]psnr",
         "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    m = re.search(r"average:([0-9.]+|inf)", out)
    return float("inf") if m and m.group(1) == "inf" else float(m.group(1)) if m else 0.0


def _encode(png: Path) -> tuple[bytes, int, float] | None:
    """Return (jpeg bytes, quality, psnr) or None to keep the PNG."""
    with tempfile.TemporaryDirectory() as tmp:
        ppm = Path(tmp) / "src.ppm"
        jpg = Path(tmp) / "out.jpg"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(png),
             "-pix_fmt", "rgb24", str(ppm)],
            check=True,
        )
        for q in QUALITIES:
            with jpg.open("wb") as fh:
                subprocess.run(
                    ["cjpeg", "-quality", str(q), "-sample", "1x1",
                     "-optimize", "-progressive", str(ppm)],
                    stdout=fh, check=True,
                )
            if jpg.stat().st_size > png.stat().st_size * MAX_RATIO:
                return None
            score = _psnr(png, jpg)
            if score >= MIN_PSNR:
                return jpg.read_bytes(), q, score
    return None


def optimize_dist(dist_assets: Path) -> dict[str, str]:
    """Convert PNG photos under dist/assets in place.

    Returns {"assets/x.png": "assets/x.jpg"} for every converted file so the
    caller can rewrite references in the generated HTML.
    """
    if not _tools_ok():
        print("optimize_images: ffmpeg/cjpeg not found — shipping original PNGs")
        return {}
    CACHE.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    before = after = 0
    for sub in PHOTO_DIRS:
        for png in sorted((dist_assets / sub).rglob("*.png")):
            key = hashlib.sha1(VERSION.encode() + png.read_bytes()).hexdigest()
            hit_jpg = CACHE / f"{key}.jpg"
            hit_keep = CACHE / f"{key}.keep"
            if not hit_jpg.exists() and not hit_keep.exists():
                if _has_alpha(png):
                    result = None
                else:
                    result = _encode(png)
                if result is None:
                    hit_keep.touch()
                else:
                    data, q, score = result
                    hit_jpg.write_bytes(data)
                    print(f"  {png.relative_to(dist_assets)}: q{q}, {score:.1f} dB, "
                          f"{png.stat().st_size // 1024}KB -> {len(data) // 1024}KB")
            size = png.stat().st_size
            before += size
            if hit_jpg.exists():
                jpg = png.with_suffix(".jpg")
                if jpg.exists():  # never overwrite a real JPEG of the same name
                    after += size
                    continue
                shutil.copy2(hit_jpg, jpg)
                png.unlink()
                after += jpg.stat().st_size
                rel = png.relative_to(dist_assets.parent).as_posix()
                mapping[rel] = jpg.relative_to(dist_assets.parent).as_posix()
            else:
                after += size
    print(f"optimize_images: {len(mapping)} PNGs -> JPEG, "
          f"{before / 1048576:.0f}MB -> {after / 1048576:.0f}MB")
    return mapping


def rewrite_refs(dist: Path, mapping: dict[str, str]) -> None:
    """Point every generated HTML page at the converted files."""
    if not mapping:
        return
    pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    for page in dist.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        new = pattern.sub(lambda m: mapping[m.group(0)], text)
        if new != text:
            page.write_text(new, encoding="utf-8")
