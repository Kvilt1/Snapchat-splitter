"""Configuration and utilities for Snapchat media mapper."""

# Standard library imports
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Third-party imports
import pytz


# Custom Exceptions
class SnapchatMapperError(Exception):
    """Base exception for Snapchat Mapper."""
    pass


class MediaProcessingError(SnapchatMapperError):
    """Raised when media processing fails."""
    pass


class ConfigurationError(SnapchatMapperError):
    """Raised when configuration is invalid."""
    pass


class TimestampExtractionError(SnapchatMapperError):
    """Raised when timestamp extraction fails."""
    pass


# Configuration
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
TIMESTAMP_THRESHOLD_SECONDS = 60
QUICKTIME_EPOCH_ADJUSTER = 2082844800

# Timezone configuration
FAROESE_TZ = pytz.timezone('Atlantic/Faroe')  # Faroese Atlantic Time (UTC-1/UTC+0 with DST)

# Performance tuning
USE_FAST_TIMESTAMP_EXTRACTION = True  # Use ffprobe instead of manual parsing

# Worker Configuration
DEFAULT_MERGE_WORKERS = 4  # CPU-bound encoding workers
DEFAULT_TIMESTAMP_WORKERS = 8  # I/O-bound ffprobe workers
MAX_BITMOJI_WORKERS = 128  # Network-bound API requests

# Thresholds
MIN_HUE_SEPARATION = 15.0  # For fallback avatar color generation

# Bitmoji Configuration
TARGET_AVATAR_SIZE = 54
BITMOJI_API_TIMEOUT = 10
BITMOJI_RETRY_TOTAL = 3
BITMOJI_BACKOFF_FACTOR = 0.4

# FFmpeg Configuration
FFMPEG_PRESET = 'ultrafast'
FFMPEG_CRF = '23'

# Logging (configured in main.py)
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
    is_merged: bool = False
    mapping_method: Optional[str] = None
    has_audio: Optional[bool] = None

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
    except (json.JSONDecodeError, OSError, IOError) as e:
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

def safe_materialize(src: Path, dst: Path, ensure_dir: bool = False) -> bool:
    """
    Efficiently materialize a file from src to dst.
    Tries: hardlink -> copy. Returns True on success.

    Args:
        src: Source file path
        dst: Destination file path
        ensure_dir: If True, ensure parent directory exists (default False for performance)
    """
    if ensure_dir:
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
    except (OSError, IOError, shutil.Error) as e:
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