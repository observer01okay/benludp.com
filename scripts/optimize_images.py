#!/usr/bin/env python3
"""Visually-lossless image optimization for the built site.

Originals in assets/ are never touched; only dist/ gets the smaller files.

- PNG photos become JPEGs, and oversized or very heavy JPEGs are re-encoded.
  Each image gets the *lowest* JPEG quality that still measures as visually
  identical to the original (PSNR >= MIN_PSNR, full 4:4:4 color, no chroma
  subsampling). Grainy frames therefore keep high quality; clean ones shrink
  more. If no setting saves enough, the original is kept as-is.
- Anything wider than MAX_WIDTH is scaled down to it first (wider than any
  screen shows these photos).
- Tiny previews for the lightbox thumbnail strip go to assets/_thumbs/.

Results are cached in .cache/images/ (keyed by file content), so only new or
changed images are re-encoded on later builds.
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
MAX_RATIO = 0.8  # keep the original unless the JPEG is at least 20% smaller
MAX_WIDTH = 2560
HEAVY_JPEG_BPP = 0.8  # bytes/pixel above which a JPEG is worth re-encoding
THUMB_DIR = "_thumbs"
# Lightbox strip boxes are 72x40 CSS px; 240x144 covers them at 3x density.
THUMB_W, THUMB_H = 240, 144
VERSION = "v2"  # bump to force re-encoding everything


def _tools_ok() -> bool:
    return all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "cjpeg"))


def _probe(path: Path) -> tuple[int, int, str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,pix_fmt", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    w, h, fmt = out.split(",")[:3]
    return int(w), int(h), fmt


def _psnr(ref: Path, test: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-i", str(ref), "-i", str(test), "-lavfi",
         "[0:v]format=rgb24[a];[1:v]format=rgb24[b];[a][b]psnr",
         "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    m = re.search(r"average:([0-9.]+|inf)", out)
    return float("inf") if m and m.group(1) == "inf" else float(m.group(1)) if m else 0.0


def _cjpeg(ppm: Path, out: Path, quality: int, sample: str = "1x1") -> None:
    with out.open("wb") as fh:
        subprocess.run(
            ["cjpeg", "-quality", str(quality), "-sample", sample,
             "-optimize", "-progressive", str(ppm)],
            stdout=fh, check=True,
        )


def _encode(src: Path, width: int) -> tuple[bytes, int, float] | None:
    """Return (jpeg bytes, quality, psnr) or None to keep the original."""
    resize = width > MAX_WIDTH
    with tempfile.TemporaryDirectory() as tmp:
        ppm = Path(tmp) / "ref.ppm"
        jpg = Path(tmp) / "out.jpg"
        vf = ["-vf", f"scale={MAX_WIDTH}:-2:flags=lanczos"] if resize else []
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), *vf,
             "-pix_fmt", "rgb24", str(ppm)],
            check=True,
        )
        for q in QUALITIES:
            _cjpeg(ppm, jpg, q)
            # A downscale must ship even if it isn't smaller; otherwise only
            # accept real savings.
            if not resize and jpg.stat().st_size > src.stat().st_size * MAX_RATIO:
                return None
            score = _psnr(ppm, jpg)
            if score >= MIN_PSNR or (resize and q == QUALITIES[-1]):
                return jpg.read_bytes(), q, score
    return None


def _cached(key: str, make) -> Path | None:
    """Return the cached JPEG for key, creating it with make() on a miss."""
    hit = CACHE / f"{key}.jpg"
    keep = CACHE / f"{key}.keep"
    if not hit.exists() and not keep.exists():
        data = make()
        if data is None:
            keep.touch()
        else:
            hit.write_bytes(data)
    return hit if hit.exists() else None


def _optimize_one(img: Path, label: str) -> Path | None:
    """Return a cached optimized JPEG for img, or None to ship it unchanged."""
    w, h, fmt = _probe(img)
    is_png = img.suffix.lower() == ".png"
    if is_png and "a" in fmt.replace("gray", ""):  # rgba, ya8, pal8 w/ alpha…
        return None
    if not is_png and w <= MAX_WIDTH and img.stat().st_size / (w * h) < HEAVY_JPEG_BPP:
        return None  # already a sensible JPEG; re-encoding would only lose

    def make() -> bytes | None:
        result = _encode(img, w)
        if result is None:
            return None
        data, q, score = result
        print(f"  {label}: q{q}, {score:.1f} dB, {w}px wide, "
              f"{img.stat().st_size // 1024}KB -> {len(data) // 1024}KB")
        return data

    key = hashlib.sha1(VERSION.encode() + img.read_bytes()).hexdigest()
    return _cached(key, make)


def _thumb(img: Path) -> Path:
    def make() -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "t.ppm"
            jpg = Path(tmp) / "t.jpg"
            # Scale so both sides cover the box (object-fit: cover crops the rest).
            wide = f"gt(iw/ih,{THUMB_W}/{THUMB_H})"
            scale = f"scale=w='if({wide},-2,{THUMB_W})':h='if({wide},{THUMB_H},-2)':flags=lanczos"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(img), "-vf", scale,
                 "-pix_fmt", "rgb24", str(ppm)],
                check=True,
            )
            _cjpeg(ppm, jpg, 85, sample="2x2")
            return jpg.read_bytes()

    key = "thumb-" + hashlib.sha1(VERSION.encode() + img.read_bytes()).hexdigest()
    return _cached(key, make)


def optimize_dist(dist_assets: Path) -> dict[str, str]:
    """Optimize photos under dist/assets in place and write lightbox thumbs.

    Returns {"assets/x.png": "assets/x.jpg"} for every file whose name changed
    so the caller can rewrite references in the generated HTML.
    """
    if not _tools_ok():
        print("optimize_images: ffmpeg/ffprobe/cjpeg not found — shipping originals, no thumbs")
        return {}
    CACHE.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    before = after = 0
    photos: list[Path] = []
    for sub in PHOTO_DIRS:
        for img in sorted((dist_assets / sub).rglob("*")):
            if img.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                continue
            size = img.stat().st_size
            before += size
            out = img
            hit = _optimize_one(img, str(img.relative_to(dist_assets)))
            if hit is not None:
                out = img.with_suffix(".jpg")
                if out != img and out.exists():  # never clobber a real JPEG of that name
                    out = img
                else:
                    shutil.copy2(hit, out)
                    if out != img:
                        img.unlink()
                        mapping[img.relative_to(dist_assets.parent).as_posix()] = (
                            out.relative_to(dist_assets.parent).as_posix()
                        )
            after += out.stat().st_size
            photos.append(out)

    thumbs = dist_assets / THUMB_DIR
    for img in photos:
        dest = thumbs / img.relative_to(dist_assets).with_suffix(".jpg")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_thumb(img), dest)

    print(f"optimize_images: {len(mapping)} renamed to .jpg, "
          f"{before / 1048576:.0f}MB -> {after / 1048576:.0f}MB, {len(photos)} lightbox thumbs")
    return mapping


_IMG_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")((?:\.\./)*/?)(assets/)([^"]+)(")')


def rewrite_refs(dist: Path, mapping: dict[str, str]) -> None:
    """Point every page at the converted files and tag images with their thumb."""
    thumbs = dist / "assets" / THUMB_DIR
    rename = (
        re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
        if mapping else None
    )

    def add_thumb(m: re.Match) -> str:
        pre, prefix, assets, rest, quote = m.groups()
        thumb_rel = Path(rest).with_suffix(".jpg").as_posix()
        tag = f"{pre}{prefix}{assets}{rest}{quote}"
        if (thumbs / thumb_rel).exists():
            tag += f' data-thumb="{prefix}{assets}{THUMB_DIR}/{thumb_rel}"'
        return tag

    for page in dist.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        new = rename.sub(lambda m: mapping[m.group(0)], text) if rename else text
        new = _IMG_SRC.sub(add_thumb, new)
        if new != text:
            page.write_text(new, encoding="utf-8")
