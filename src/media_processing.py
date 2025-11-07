"""Media processing: overlay merging, indexing, and mapping."""

import hashlib
import json
import logging
import os
import re
import shutil
import struct
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from bisect import bisect_left, bisect_right

from config import (
    TIMESTAMP_THRESHOLD_SECONDS,
    QUICKTIME_EPOCH_ADJUSTER,
    ensure_directory,
    MediaFile,
    Stats
)

# Direct ffmpeg-python import for overlay merging
import ffmpeg

logger = logging.getLogger(__name__)


def create_media_folder(media_file: Path, overlay_file: Path, output_dir: Path) -> bool:
    """
    Create folder structure containing video + overlay.

    Structure:
    output_dir/
      └── {media_filename}/
          ├── video{.ext}
          └── overlay{.ext}

    Returns:
        True if folder created successfully, False otherwise.
    """
    try:
        # Extract base name (without extension)
        folder_name = media_file.stem
        folder_path = output_dir / folder_name

        # Create folder
        ensure_directory(folder_path)

        # Copy video with generic name
        video_dest = folder_path / f"video{media_file.suffix}"
        shutil.copy2(media_file, video_dest)

        # Copy overlay with generic name
        overlay_dest = folder_path / f"overlay{overlay_file.suffix}"
        shutil.copy2(overlay_file, overlay_dest)

        return True
    except Exception as e:
        logger.error(f"Error creating folder for {media_file.name}: {e}")
        return False


def organize_overlay_pairs(source_dir: Path, output_dir: Path) -> Tuple[Set[str], Dict[str, Any]]:
    """Create folders containing video + overlay pairs instead of merging."""
    logger.info("=" * 60)
    logger.info("Starting OVERLAY ORGANIZATION phase")
    logger.info("=" * 60)

    organized_dir = output_dir / "organized_media"
    ensure_directory(organized_dir)

    stats = {'total_media': 0, 'total_overlay': 0, 'total_organized': 0}
    organized_files = set()

    # Group files by date
    files_by_date = defaultdict(lambda: {"media": [], "overlay": []})
    for file_path in source_dir.iterdir():
        if not file_path.is_file():
            continue

        match = re.match(r"(\d{4}-\d{2}-\d{2})", file_path.name)
        if not match:
            continue

        date_str = match.group(1)
        name_lower = file_path.name.lower()

        if "thumbnail" in name_lower:
            continue

        # Include both _media~ and media~zip- patterns
        if "_media~" in file_path.name or "media~zip-" in file_path.name:
            files_by_date[date_str]["media"].append(file_path)
            stats['total_media'] += 1
        elif "_overlay~" in file_path.name:
            files_by_date[date_str]["overlay"].append(file_path)
            stats['total_overlay'] += 1

    # Process each date group
    with tqdm(total=stats['total_media'], desc="Organizing media", unit="files") as pbar:
        for date_str, files in files_by_date.items():
            media_files = sorted(files["media"], key=lambda x: x.name)
            overlay_files = sorted(files["overlay"], key=lambda x: x.name)

            if not media_files or not overlay_files:
                continue

            # Check file size first (fast) to determine pattern
            if len(overlay_files) == 1 or (len(overlay_files) > 1 and
                len(set(f.stat().st_size for f in overlay_files)) == 1):
                # Single/multipart: use first overlay for all media
                overlay = overlay_files[0]
                for media in media_files:
                    if create_media_folder(media, overlay, organized_dir):
                        organized_files.add(media.name)
                        organized_files.add(overlay.name)
                        stats['total_organized'] += 1
                    pbar.update(1)
            else:
                # Grouped: pair each media with its overlay
                for media, overlay in zip(media_files, overlay_files):
                    if create_media_folder(media, overlay, organized_dir):
                        organized_files.add(media.name)
                        organized_files.add(overlay.name)
                        stats['total_organized'] += 1
                    pbar.update(1)

    logger.info(f"Organized {stats['total_organized']} media files into folders")
    logger.info("=" * 60)
    return organized_files, stats

def find_video_in_folder(folder: Path) -> Optional[Path]:
    """Find video.* file in folder."""
    for ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm']:
        video = folder / f"video{ext}"
        if video.exists():
            return video
    return None


def find_overlay_in_folder(folder: Path) -> Optional[Path]:
    """Find overlay.* file in folder."""
    for ext in ['.webp', '.png', '.jpg', '.jpeg']:
        overlay = folder / f"overlay{ext}"
        if overlay.exists():
            return overlay
    return None


def extract_media_id(filename: str) -> Optional[str]:
    """Extract media ID from filename."""
    if 'thumbnail' in filename.lower():
        return None

    if 'b~' in filename:
        match = re.search(r'b~([^.]+)', filename, re.I)
        if match:
            return f'b~{match.group(1)}'

    match = re.search(r'media~zip-([A-F0-9\-]+)', filename, re.I)
    if match:
        return f'media~zip-{match.group(1)}'

    match = re.search(r'(media|overlay)~([A-F0-9\-]+)', filename, re.I)
    if match:
        return f'{match.group(1)}~{match.group(2)}'

    return None

def index_media_files(source_dir: Path, organized_dir: Optional[Path] = None) -> Tuple[Dict[str, MediaFile], Dict]:
    """Create index of all media files from source and organized directories."""
    logger.info("=" * 60)
    logger.info("Starting MEDIA INDEXING phase")
    logger.info("=" * 60)

    media_index = {}
    stats = {'total_files': 0, 'extracted_ids': 0}

    # Count total files first for progress bar
    source_files = [f for f in source_dir.iterdir()
                   if f.is_file() and "thumbnail" not in f.name.lower() and "_overlay~" not in f.name]

    organized_folders = []
    if organized_dir and organized_dir.exists():
        organized_folders = [f for f in organized_dir.iterdir() if f.is_dir()]

    total_items = len(source_files) + len(organized_folders)

    # Index source files with progress bar (timestamps extracted lazily later)
    with tqdm(total=total_items, desc="Indexing media files", unit="files") as pbar:
        for item in source_files:
            stats['total_files'] += 1
            media_id = extract_media_id(item.name)

            media_file = MediaFile(
                filename=item.name,
                source_path=item,
                media_id=media_id,
                timestamp=None,  # Extract lazily only when needed for timestamp mapping
                is_folder=False
            )

            if media_id:
                media_index[media_id] = media_file
                stats['extracted_ids'] += 1

            pbar.update(1)

        # Index organized folders - these take precedence over source files
        for folder in organized_folders:
            stats['total_files'] += 1

            # Find video file inside folder
            video_file = find_video_in_folder(folder)
            overlay_file = find_overlay_in_folder(folder)

            if video_file:
                media_id = extract_media_id(folder.name)

                media_file = MediaFile(
                    filename=folder.name,
                    source_path=folder,
                    media_id=media_id,
                    timestamp=None,  # Extract lazily only when needed for timestamp mapping
                    is_merged=True,  # Keep for compatibility
                    is_folder=True,
                    video_path=video_file,
                    overlay_path=overlay_file
                )

                if media_id:
                    media_index[media_id] = media_file  # Organized folders take precedence
                    stats['extracted_ids'] += 1

            pbar.update(1)

    logger.info(f"Indexed {stats['total_files']} items, extracted {stats['extracted_ids']} IDs")
    logger.info("=" * 60)

    return media_index, stats

def extract_mp4_timestamp_fast(mp4_path: Path) -> Optional[int]:
    """Extract creation timestamp using ffprobe."""
    try:
        probe = ffmpeg.probe(str(mp4_path))
        
        # Try format tags
        if 'format' in probe and 'tags' in probe['format']:
            creation_time = probe['format']['tags'].get('creation_time')
            if creation_time:
                dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                return int(dt.timestamp() * 1000)
        
        # Try streams
        if 'streams' in probe:
            for stream in probe['streams']:
                if stream.get('codec_type') == 'video':
                    creation_time = stream.get('tags', {}).get('creation_time')
                    if creation_time:
                        dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                        return int(dt.timestamp() * 1000)
        
        return None
        
    except Exception as e:
        logger.debug(f"Could not extract timestamp from {mp4_path}: {e}")
        return None


def has_audio_stream(video_path: Path) -> bool:
    """
    Check if a video file has an audio stream.

    Args:
        video_path: Path to video file

    Returns:
        True if video has audio stream, False otherwise
    """
    try:
        probe_result = ffmpeg.probe(str(video_path))
        return any(stream['codec_type'] == 'audio' for stream in probe_result['streams'])
    except (ffmpeg.Error, KeyError, Exception) as e:
        logger.debug(f"Could not detect audio stream for {video_path}: {e}")
        return False


def has_video_stream(video_path: Path) -> bool:
    """
    Check if file has a video stream.

    Used to distinguish voice notes (audio-only) from regular videos.

    Args:
        video_path: Path to video file

    Returns:
        True if file has video stream, False if audio-only (voice note)
    """
    try:
        probe = ffmpeg.probe(str(video_path))
        return any(s.get('codec_type') == 'video' for s in probe.get('streams', []))
    except Exception as e:
        logger.debug(f"Could not check video stream for {video_path}: {e}")
        return True  # Default to video if probe fails


def extract_media_metadata(video_path: Path) -> Tuple[Optional[int], Optional[bool]]:
    """
    Extract both timestamp and audio info in a single ffmpeg.probe call.

    Combines logic from extract_mp4_timestamp_fast() and has_audio_stream()
    to reduce subprocess calls by 50%.

    Args:
        video_path: Path to video file

    Returns:
        (timestamp_ms, has_audio) - both can be None if extraction fails
    """
    try:
        probe = ffmpeg.probe(str(video_path))

        # Extract timestamp (from extract_mp4_timestamp_fast logic)
        timestamp = None
        if 'format' in probe and 'tags' in probe['format']:
            creation_time = probe['format']['tags'].get('creation_time')
            if creation_time:
                dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp() * 1000)

        # Try streams if format tags didn't have timestamp
        if not timestamp and 'streams' in probe:
            for stream in probe['streams']:
                if stream.get('codec_type') == 'video':
                    creation_time = stream.get('tags', {}).get('creation_time')
                    if creation_time:
                        dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                        timestamp = int(dt.timestamp() * 1000)
                        break

        # Extract audio info (from has_audio_stream logic)
        has_audio = any(stream.get('codec_type') == 'audio'
                       for stream in probe.get('streams', []))

        return timestamp, has_audio

    except Exception as e:
        logger.debug(f"Could not extract metadata from {video_path}: {e}")
        return None, None


def build_timestamp_index(conversations: Dict[str, List]) -> Tuple[List[int], Dict[int, List[Tuple]]]:
    """
    Build optimized index for O(log n) timestamp-based media mapping.
    
    Returns:
        sorted_timestamps: Sorted list of all message timestamps
        timestamp_to_messages: Dict mapping timestamp to list of (conv_id, msg_idx)
    """
    timestamp_to_messages = defaultdict(list)
    
    for conv_id, messages in conversations.items():
        for i, msg in enumerate(messages):
            ts = int(msg.get("Created(microseconds)", 0))
            if ts > 0:
                timestamp_to_messages[ts].append((conv_id, i))
    
    # Sort timestamps for binary search
    sorted_timestamps = sorted(timestamp_to_messages.keys())
    
    return sorted_timestamps, timestamp_to_messages


def find_timestamp_matches(media_timestamp: int, 
                          sorted_timestamps: List[int],
                          timestamp_to_messages: Dict[int, List[Tuple]],
                          threshold_ms: int) -> List[Tuple[str, int, int, int]]:
    """
    Use binary search to find timestamp matches in O(log n) time.
    
    Returns:
        List of (conv_id, msg_idx, msg_ts, diff) sorted by time difference
    """
    if not media_timestamp:
        return []
    
    # Find range using binary search - O(log n)
    lower_bound = media_timestamp - threshold_ms
    upper_bound = media_timestamp + threshold_ms
    
    left_idx = bisect_left(sorted_timestamps, lower_bound)
    right_idx = bisect_right(sorted_timestamps, upper_bound)
    
    # Collect all matches in range
    potential_matches = []
    for idx in range(left_idx, right_idx):
        ts = sorted_timestamps[idx]
        diff = abs(media_timestamp - ts)
        for conv_id, msg_idx in timestamp_to_messages[ts]:
            potential_matches.append((conv_id, msg_idx, ts, diff))
    
    # Sort by time difference
    potential_matches.sort(key=lambda x: x[3])
    return potential_matches

def map_media_to_messages(conversations: Dict[str, List], media_index: Dict[str, MediaFile]) -> Tuple[Set[str], Dict]:
    """Map media files to conversation messages by attaching directly to message dicts."""
    logger.info("=" * 60)
    logger.info("Starting MEDIA MAPPING phase")
    logger.info("=" * 60)

    mapped_files = set()
    stats = {'mapped_by_id': 0, 'mapped_by_timestamp': 0, 'fallback_snap_used': 0}

    # Phase 1: Map by Media ID
    logger.info("Phase 1: Mapping by Media ID...")
    
    # Count total messages for progress tracking
    total_messages = sum(len(messages) for messages in conversations.values())
    
    with tqdm(total=total_messages, desc="Mapping by Media ID", unit="msgs") as pbar:
        for conv_id, messages in conversations.items():
            for msg in messages:
                media_ids_str = msg.get("Media IDs", "")
                if not media_ids_str:
                    pbar.update(1)
                    continue

                media_ids = [mid.strip() for mid in media_ids_str.split('|')]
                msg_type = msg.get("Type", "")
                
                # For snap messages, only map the first Media ID
                if msg_type == "snap":
                    media_ids = media_ids[:1]

                for media_id in media_ids:
                    if media_id in media_index:
                        media_file = media_index[media_id]

                        msg.setdefault('_media', []).append({
                            "media_file": media_file,
                            "mapping_method": "media_id"
                        })
                        mapped_files.add(media_file.filename)
                        stats['mapped_by_id'] += 1
                
                pbar.update(1)

    # Phase 2: Map unmapped files by timestamp using BINARY SEARCH
    logger.info("Phase 2: Mapping by timestamp (optimized with binary search)...")

    # Build optimized timestamp index - O(n log n)
    sorted_timestamps, timestamp_to_messages = build_timestamp_index(conversations)
    logger.debug(f"Built timestamp index with {len(sorted_timestamps)} unique timestamps")

    # Map unmapped files with timestamps
    threshold_ms = TIMESTAMP_THRESHOLD_SECONDS * 1000
    
    # Count unmapped MP4 files (need timestamps) - handle both files and folders
    unmapped_mp4s = []
    for mf in media_index.values():
        if mf.filename not in mapped_files:
            # Check if it's a folder with video or a direct MP4 file
            if mf.is_folder and mf.video_path:
                unmapped_mp4s.append(mf)
            elif not mf.is_folder and mf.source_path.suffix.lower() == '.mp4':
                unmapped_mp4s.append(mf)

    logger.info(f"Extracting timestamps from {len(unmapped_mp4s)} unmapped MP4 files...")

    # Extract timestamps in parallel for unmapped MP4s only
    def extract_timestamp_worker(media_file: MediaFile) -> Tuple[str, Optional[int]]:
        # Use video_path if folder-based, otherwise source_path
        video_path = media_file.video_path if media_file.is_folder else media_file.source_path
        ts = extract_mp4_timestamp_fast(video_path)
        return (media_file.filename, ts)
    
    # Parallel timestamp extraction
    timestamp_map = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_file = {executor.submit(extract_timestamp_worker, mf): mf for mf in unmapped_mp4s}
        
        with tqdm(total=len(unmapped_mp4s), desc="Extracting timestamps", unit="files") as ts_pbar:
            for future in as_completed(future_to_file):
                filename, timestamp = future.result()
                if timestamp:
                    timestamp_map[filename] = timestamp
                ts_pbar.update(1)
    
    # Apply timestamps to media files
    for mf in media_index.values():
        if mf.filename in timestamp_map:
            mf.timestamp = timestamp_map[mf.filename]
    
    # Get unmapped files that now have timestamps
    unmapped_with_ts = [mf for mf in unmapped_mp4s if mf.timestamp]
    
    with tqdm(total=len(unmapped_with_ts), desc="Mapping by timestamp", unit="files") as pbar:
        for media_file in unmapped_with_ts:
            # Find matches using binary search - O(log n)
            potential_matches = find_timestamp_matches(
                media_file.timestamp,
                sorted_timestamps,
                timestamp_to_messages,
                threshold_ms
            )
            
            if not potential_matches:
                pbar.update(1)
                continue
            
            best_match = None
            min_diff = float('inf')
            fallback_match = None  # For locked snaps as last resort
            fallback_diff = float('inf')
            
            # Find the best available match (prioritize empty snaps)
            for conv_id, msg_idx, msg_ts, diff in potential_matches:
                # Get the actual message to check its type
                if conv_id in conversations and msg_idx < len(conversations[conv_id]):
                    msg = conversations[conv_id][msg_idx]
                    msg_type = msg.get("Type", "")
                    
                    # For snap messages, check if already has media mapped
                    if msg_type == "snap":
                        if msg.get('_media'):
                            # This snap already has media - keep as fallback if no empty snaps found
                            if fallback_match is None or diff < fallback_diff:
                                fallback_match = (conv_id, msg_idx)
                                fallback_diff = diff
                            continue
                    
                    # This is a valid match (empty message or non-snap)
                    best_match = (conv_id, msg_idx)
                    min_diff = diff
                    break
            
            # If no available match found, check if we can use a locked snap to prevent orphaning
            if not best_match and fallback_match:
                conv_id, msg_idx = fallback_match
                # Only use fallback if it's a snap and would prevent orphaning
                if (conv_id in conversations and msg_idx < len(conversations[conv_id]) and
                    conversations[conv_id][msg_idx].get("Type") == "snap"):
                    best_match = fallback_match
                    min_diff = fallback_diff
                    logger.debug(f"Using locked snap as fallback for {media_file.filename} to prevent orphaning")
                
            # If using fallback, log this for transparency
            if best_match and fallback_match and best_match == fallback_match:
                logger.debug(f"Media {media_file.filename} added to already-occupied snap to prevent orphaning")
                stats['fallback_snap_used'] += 1

            if best_match and min_diff <= threshold_ms:
                conv_id, msg_idx = best_match
                msg = conversations[conv_id][msg_idx]

                msg.setdefault('_media', []).append({
                    "media_file": media_file,
                    "mapping_method": "timestamp",
                    "time_diff_seconds": round(min_diff / 1000.0, 1)
                })
                mapped_files.add(media_file.filename)
                stats['mapped_by_timestamp'] += 1
            
            pbar.update(1)

    logger.info(f"Mapped {stats['mapped_by_id']} by ID, {stats['mapped_by_timestamp']} by timestamp")
    if stats['fallback_snap_used'] > 0:
        logger.info(f"Used {stats['fallback_snap_used']} fallback mappings (2nd media on snap to prevent orphaning)")
    logger.info("=" * 60)

    return mapped_files, stats