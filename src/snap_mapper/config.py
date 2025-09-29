"""Configuration constants for Snapchat Media Mapper."""

import logging
from pathlib import Path

# Directory configuration
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")

# Processing thresholds and constants
TIMESTAMP_THRESHOLD_SECONDS = 60
QUICKTIME_EPOCH_ADJUSTER = 2082844800

# Cache directory for converted files
CACHE_DIR = Path(".cache")

# Logging configuration
DEFAULT_LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'

# Processing configuration
DEFAULT_MAX_WORKERS = 8
DEFAULT_ENCODING = 'utf-8'

# File extensions
SUPPORTED_VIDEO_FORMATS = {'.mp4', '.mov', '.avi'}
SUPPORTED_IMAGE_FORMATS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
SUPPORTED_MEDIA_FORMATS = SUPPORTED_VIDEO_FORMATS | SUPPORTED_IMAGE_FORMATS
