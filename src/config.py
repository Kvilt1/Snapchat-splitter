"""Configuration and utilities for Snapchat media mapper."""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import pytz

# Configuration
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
TIMESTAMP_THRESHOLD_SECONDS = 75  # Increased to handle Bitmoji video timing offsets
QUICKTIME_EPOCH_ADJUSTER = 2082844800

# Timezone configuration
FAROESE_TZ = pytz.timezone('Atlantic/Faroe')  # Faroese Atlantic Time (UTC-1/UTC+0 with DST)

# Performance tuning
USE_FAST_TIMESTAMP_EXTRACTION = True  # Use ffprobe instead of manual parsing

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Media type mappings
MEDIA_TYPE_MAP = {
    'IMAGE': ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'],
    'VIDEO': ['mp4', 'mov', 'avi', 'mkv', 'webm'],
    'AUDIO': ['mp3', 'wav', 'aac', 'm4a', 'ogg']
}

@dataclass
class MediaFile:
    """Represents a media file with its metadata."""
    filename: str
    source_path: Path
    media_id: Optional[str] = None
    timestamp: Optional[int] = None
    is_merged: bool = False  # Deprecated: kept for compatibility, same as is_folder
    mapping_method: Optional[str] = None
    has_audio: Optional[bool] = None
    is_folder: bool = False  # True if source_path points to organized folder
    video_path: Optional[Path] = None  # Path to video file inside folder
    overlay_path: Optional[Path] = None  # Path to overlay file inside folder

@dataclass
class Stats:
    """Centralized statistics tracking."""
    # Organization stats (renamed from Merge stats)
    total_media: int = 0
    total_overlay: int = 0
    total_organized: int = 0  # Renamed from total_merged

    # Mapping stats
    mapped_by_id: int = 0
    mapped_by_timestamp: int = 0
    rescued_orphans: int = 0
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


def utc_to_faroese(utc_timestamp_ms: int) -> datetime:
    """
    Convert UTC timestamp (milliseconds) to Faroese Atlantic Time.
    
    Args:
        utc_timestamp_ms: UTC timestamp in milliseconds since epoch
        
    Returns:
        datetime object in Faroese timezone
    """
    # Convert milliseconds to seconds
    utc_dt = datetime.fromtimestamp(utc_timestamp_ms / 1000.0, tz=timezone.utc)
    # Convert to Faroese time
    faroese_dt = utc_dt.astimezone(FAROESE_TZ)
    return faroese_dt


def format_faroese_timestamp(faroese_dt: datetime) -> str:
    """
    Format Faroese datetime with millisecond precision.
    
    Args:
        faroese_dt: datetime in Faroese timezone
        
    Returns:
        String in format: "YYYY-MM-DD HH:MM:SS.mmm Atlantic/Faroe"
    """
    # Format with milliseconds
    ms = faroese_dt.microsecond // 1000
    return f"{faroese_dt.strftime('%Y-%m-%d %H:%M:%S')}.{ms:03d} Atlantic/Faroe"


def get_faroese_date(utc_timestamp_ms: int) -> str:
    """
    Get the Faroese date (YYYY-MM-DD) for a UTC timestamp.
    
    Args:
        utc_timestamp_ms: UTC timestamp in milliseconds
        
    Returns:
        Date string in format YYYY-MM-DD (Faroese timezone)
    """
    faroese_dt = utc_to_faroese(utc_timestamp_ms)
    return faroese_dt.strftime('%Y-%m-%d')


def get_media_type(extension: str) -> str:
    """Determine media type from file extension.
    
    Args:
        extension: File extension (with or without dot)
        
    Returns:
        Media type string: 'IMAGE', 'VIDEO', 'AUDIO', or 'UNKNOWN'
    """
    ext = extension.lstrip('.').lower()
    for media_type, extensions in MEDIA_TYPE_MAP.items():
        if ext in extensions:
            return media_type
    return "UNKNOWN"