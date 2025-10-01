#!/usr/bin/env python3
"""Bitmoji processing helpers for fetching and generating avatar SVGs."""

import colorsys
import hashlib
import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
import threading
from typing import Dict, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import sanitize_filename

logger = logging.getLogger(__name__)

# The final size of the avatar SVG
TARGET_SIZE = 54
# Maximum number of concurrent download threads
MAX_WORKERS = 128
# Path for the default "ghost" avatar icon
FALLBACK_AVATAR_PATH = (
    "M27 54.06C33.48 54.06 39.48 51.78 44.16 47.94C43.32 46.68 42.36 45.78 41.34 44.94C38.22 42.48 "
    "33.78 41.58 30.72 41.04L30.6 39.84C35.28 37.08 36.42 34.14 38.28 27.96L38.34 27.54C38.34 27.54 "
    "39.96 26.88 40.2 23.88C40.56 19.8 38.88 21 38.88 20.7C39.06 18.6 39 15.84 38.4 13.8C37.14 9.42 "
    "32.88 5.94 27 5.94C21.12 5.94 16.86 9.36 15.6 13.8C15 15.84 14.94 18.6 15.12 20.76C15.12 21.06 "
    "13.5 19.86 13.8 23.94C14.04 26.94 15.66 27.6 15.66 27.6L15.72 28.02C17.58 34.2 18.72 37.14 "
    "23.4 39.9L23.28 41.1C20.28 41.64 15.78 42.54 12.66 45C11.64 45.84 10.68 46.74 9.84 48C14.52 "
    "51.78 20.52 54.06 27 54.06Z"
)
# XML namespaces for SVG parsing
SVG_NS = {"svg": "http://www.w3.org/2000/svg", "xlink": "http://www.w3.org/1999/xlink"}

_thread_local = threading.local()


def _build_session() -> requests.Session:
    """Configure a session with retry-aware adapters and large connection pools."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "snapchat-media-mapper/1.0"})
    return session


def _get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _build_session()
        _thread_local.session = session
    return session


class FallbackGenerator:
    """Generates unique, visually distinct fallback avatars for usernames."""

    def __init__(self):
        self._assigned_colors: Dict[str, str] = {}
        self._assigned_hues: Set[float] = set()
        self._lock = Lock()

    def _get_distinct_color(self, username: str) -> str:
        """Generate a unique color for a username that stays visually distinct."""
        with self._lock:
            if username in self._assigned_colors:
                return self._assigned_colors[username]

            hash_bytes = hashlib.sha256(username.encode("utf-8")).digest()
            base_hue = int.from_bytes(hash_bytes[:2], "little") % 360

            hue = base_hue
            attempt = 0
            min_separation = 15.0  # Minimum distance between hues on the color wheel

            while any(abs(hue - h) < min_separation for h in self._assigned_hues):
                hue = (base_hue + 137.508 * attempt) % 360.0
                attempt += 1
                if attempt > 360:
                    break

            self._assigned_hues.add(hue)

            r, g, b = colorsys.hls_to_rgb(hue / 360.0, 0.60, 0.30)
            color_hex = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

            self._assigned_colors[username] = color_hex
            return color_hex

    def build(self, username: str) -> str:
        """Build the complete fallback SVG string for a username."""
        fill_color = self._get_distinct_color(username)
        return (
            f'<svg viewBox="0 0 {TARGET_SIZE} {TARGET_SIZE}" xmlns="http://www.w3.org/2000/svg">'
            f'<path d="{FALLBACK_AVATAR_PATH}" fill="{fill_color}" '
            'stroke="black" stroke-opacity="0.2" stroke-width="0.9"/>'
            '</svg>'
        )


fallback_generator = FallbackGenerator()


def get_avatar(username: str) -> tuple[str, str, str]:
    """Fetch, process, and normalize a Bitmoji avatar, with fallback on failure."""
    try:
        params = {"username": username, "type": "SVG", "bitmoji": "enable"}
        session = _get_session()
        response = session.get("https://app.snapchat.com/web/deeplink/snapcode", params=params, timeout=10)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        image_element = root.find(".//svg:image", SVG_NS)

        if image_element is None:
            raise ValueError("No <image> element found in Snapcode SVG.")

        href = image_element.get(f"{{{SVG_NS['xlink']}}}href") or image_element.get("href")
        if not href:
            raise ValueError("No href attribute found on <image> element.")

        clean_svg = (
            f'<svg viewBox="0 0 {TARGET_SIZE} {TARGET_SIZE}" xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<image href="{href}" x="0" y="0" width="{TARGET_SIZE}" height="{TARGET_SIZE}"/>'
            '</svg>'
        )
        return username, clean_svg, "success"

    except (requests.RequestException, ET.ParseError, ValueError) as exc:
        logger.warning("Could not get Bitmoji for '%s': %s. Using fallback.", username, exc)
        return username, fallback_generator.build(username), "fallback"


def get_all_avatars(usernames: Set[str]) -> Dict[str, str]:
    """Fetch Bitmoji avatars for multiple usernames concurrently."""
    if not usernames:
        return {}

    logger.info("Starting Bitmoji extraction for %d users...", len(usernames))
    avatars: Dict[str, str] = {}
    stats = {"success": 0, "fallback": 0}

    max_workers = min(MAX_WORKERS, max(1, len(usernames)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(get_avatar, name): name for name in usernames}
        for future in as_completed(futures):
            username, svg_content, status = future.result()
            if svg_content:
                avatars[username] = svg_content
                stats[status] += 1

    logger.info(
        "Extraction complete: %d successful, %d fallbacks (workers=%d).",
        stats["success"],
        stats["fallback"],
        max_workers,
    )
    return avatars


def save_avatars(avatars: Dict[str, str], output_dir: Path) -> Dict[str, str]:
    """Save SVG avatars to a directory, keyed by username-based filenames."""
    output_dir.mkdir(parents=True, exist_ok=True)
    user_to_path: Dict[str, str] = {}
    used_names: Dict[str, str] = {}

    for username in sorted(avatars):
        svg_content = avatars[username]
        sanitized = sanitize_filename(username)
        base_name = sanitized.strip().lower() if sanitized else "user"
        if not base_name:
            base_name = "user"

        candidate = base_name
        if candidate in used_names and used_names[candidate] != username:
            suffix = hashlib.sha1(username.encode("utf-8")).hexdigest()[:6]
            candidate = f"{base_name}-{suffix}"
        used_names[candidate] = username

        filename = f"{candidate}.svg"
        filepath = output_dir / filename
        filepath.write_text(svg_content, encoding="utf-8")

        user_to_path[username] = f"bitmoji/{filename}"

    logger.info("Saved %d avatars to '%s'.", len(user_to_path), output_dir)
    return user_to_path


def generate_bitmoji_assets(usernames: Set[str], output_root: Path) -> Dict[str, str]:
    """Fetch avatars (fallbacks as needed) and persist them, returning relative paths."""
    if not usernames:
        logger.info("No usernames provided, skipping Bitmoji generation.")
        return {}

    avatars = get_all_avatars(usernames)
    return save_avatars(avatars, output_root / "bitmoji")


if __name__ == "__main__":
    sample_usernames = {
        "snapchat",
        "drax",
        "gallowboob",
        "spez",
        "bad-username-that-does-not-exist",
        "another-fake-user",
        "testuser12345",
    }

    output_dir = Path("avatar_pool")
    paths = generate_bitmoji_assets(sample_usernames, output_dir)
    for username, relative in paths.items():
        print(f"{username}: {relative}")