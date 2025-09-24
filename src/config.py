"""Configuration and utilities for Snapchat media mapper."""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# Configuration
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
TIMESTAMP_THRESHOLD_SECONDS = 60
QUICKTIME_EPOCH_ADJUSTER = 2082844800

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class MediaFile:
    """Represents a media file with its metadata."""
    filename: str
    source_path: Path
    media_id: Optional[str] = None
    timestamp: Optional[int] = None
    is_merged: bool = False
    is_grouped: bool = False
    mapping_method: Optional[str] = None

@dataclass
class Stats:
    """Centralized statistics tracking."""
    # Merge stats
    total_media: int = 0
    total_overlay: int = 0
    total_merged: int = 0

    # Mapping stats
    mapped_by_id: int = 0
    mapped_by_timestamp: int = 0
    orphaned: int = 0

    # Timing
    phase_times: Dict[str, float] = field(default_factory=dict)

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
    import re
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