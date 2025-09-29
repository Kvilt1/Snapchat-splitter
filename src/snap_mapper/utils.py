"""Utility functions for Snapchat Media Mapper."""

import json
import logging
import os
import shutil
import re
from pathlib import Path
from typing import Dict, Any


logger = logging.getLogger(__name__)


def ensure_directory(path: Path) -> None:
    """Ensure directory exists."""
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Dict[str, Any]:
    """Load JSON file, return empty dict on error."""
    if not path.exists():
        logger.error(f"JSON file not found: {path}")
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return {}


def save_json(data: Dict[str, Any], path: Path) -> None:
    """Save dictionary to JSON file."""
    ensure_directory(path.parent)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename."""
    return re.sub(r'[\\/*?:"<>|]', "", filename)[:255]


def safe_materialize(src: Path, dst: Path) -> bool:
    """
    Efficiently materialize a file from src to dst.
    Tries: hardlink -> copy. Returns True on success.
    """
    ensure_directory(dst.parent)

    # Don't copy if already exists
    if dst.exists():
        return True

    try:
        # Try hardlink first (instant, no extra space)
        try:
            os.link(src, dst)
            return True
        except (OSError, NotImplementedError):
            pass

        # Fallback to copy
        if src.is_file():
            shutil.copy2(src, dst)
        else:
            shutil.copytree(src, dst)
        return True
    except Exception as e:
        logger.error(f"Failed to materialize {src} to {dst}: {e}")
        return False
