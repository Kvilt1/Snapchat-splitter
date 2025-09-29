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

def cleanup_process_pool():
    """Cleanup function for compatibility with existing code."""
    # No cleanup needed for direct ffmpeg-python usage
    pass

def run_ffmpeg_merge(media_file: Path, overlay_file: Path, output_path: Path, 
                    allow_overwriting: bool = True, quiet: bool = True) -> bool:
    """
    Merge media with overlay using direct ffmpeg-python.
    Returns True on success, False on failure.
    """
    try:
        ensure_directory(output_path.parent)
        
        # Create video input
        vid = ffmpeg.input(str(media_file))
        
        # Process with overlay
        overlay_img = ffmpeg.input(str(overlay_file))
        # Use scale to scale the overlay to match video height, keeping aspect ratio
        # "rh" means reference height from the main video stream
        scaled = overlay_img.filter("scale", "-1", "rh")
        # Overlay the overlay onto the video
        overlay_video = vid.overlay(scaled, eof_action="repeat")
        
        # Create output with video and audio, preserving metadata
        output_node = ffmpeg.output(
            overlay_video,  # video
            vid.audio,      # audio
            str(output_path),  # output file
            # Preserve all metadata from input streams
            map_metadata=0,  # Copy metadata from input stream 0 (the video)
            movflags="+faststart"  # Move moov atom to beginning for faster loading
        )
        
        if allow_overwriting:
            output_node = output_node.overwrite_output()
            
        logger.debug(f"Running ffmpeg merge: {media_file.name} + {overlay_file.name} -> {output_path.name}")
        
        # Run the conversion
        output_node.run(quiet=quiet)
        return True
        
    except ffmpeg.Error as err:
        logger.error(f"ffmpeg error merging {media_file.name} with overlay {overlay_file.name}: {err}")
        return False
    except Exception as e:
        logger.error(f"Error merging {media_file.name} with overlay {overlay_file.name}: {e}")
        return False

def overlay_merge_single(media_file: Path, overlay_file: Path, output_path: Path) -> bool:
    """
    Merge media with overlay using direct ffmpeg-python.
    Wrapper for run_ffmpeg_merge that preserves original file timestamps.
    """
    if run_ffmpeg_merge(media_file, overlay_file, output_path):
        # Preserve original timestamps
        if output_path.exists():
            st = media_file.stat()
            os.utime(output_path, (st.st_atime, st.st_mtime))
        return True
    return False

def overlay_worker(args: Tuple[Path, Path, Path]) -> Optional[Tuple[str, Optional[int]]]:
    """Worker function for overlay merging using direct ffmpeg-python."""
    media_file, overlay_file, output_path = args
    if overlay_merge_single(media_file, overlay_file, output_path):
        timestamp = extract_mp4_timestamp(media_file)
        return (media_file.name, timestamp)
    return None

def calculate_file_hash(file_path: Path) -> Optional[str]:
    """Calculate MD5 hash of file."""
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None


def merge_overlay_pairs(source_dir: Path, output_dir: Path) -> Tuple[Set[str], Dict[str, Any]]:
    """Find and merge media/overlay pairs using direct ffmpeg-python processing."""
    logger.info("=" * 60)
    logger.info("Starting OVERLAY MERGING phase")
    logger.info("=" * 60)

    stats = {
        'total_media': 0,
        'total_overlay': 0,
        'total_merged': 0
    }

    merged_dir = output_dir / "merged_media"
    ensure_directory(merged_dir)

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

        if "thumbnail" in name_lower or "media~zip-" in file_path.name:
            continue

        if "_media~" in file_path.name:
            files_by_date[date_str]["media"].append(file_path)
            stats['total_media'] += 1
        elif "_overlay~" in file_path.name:
            files_by_date[date_str]["overlay"].append(file_path)
            stats['total_overlay'] += 1

    logger.info(f"Found {stats['total_media']} media files and {stats['total_overlay']} overlay files")

    merged_files = set()

    for date_str, files in files_by_date.items():
        media_files = files["media"]
        overlay_files = files["overlay"]

        if len(media_files) == 1 and len(overlay_files) == 1:
            # Simple pair - merge directly
            try:
                output_file = merged_dir / media_files[0].name
                if overlay_merge_single(media_files[0], overlay_files[0], output_file):
                    merged_files.add(media_files[0].name)
                    merged_files.add(overlay_files[0].name)
                    stats['total_merged'] += 1
            except Exception as e:
                logger.error(f"Error merging single pair for {date_str}: {e}")

        elif len(overlay_files) > 0 and len(media_files) > 0:
            # Handle grouped media individually
            media_sorted = sorted(media_files, key=lambda x: x.name)
            overlay_sorted = sorted(overlay_files, key=lambda x: x.name)
            
            # Check if overlays are identical (multipart) or different (grouped)
            if len(overlay_files) == 1 or (len(overlay_files) > 1 and
                len(set(calculate_file_hash(f) for f in overlay_files)) == 1):
                # Multipart: same overlay for all media files
                overlay = overlay_files[0]  # Use the first (and same) overlay
                logger.info(f"Processing multipart group for {date_str}: {len(media_files)} files with same overlay")
                
                for media_file in media_sorted:
                    try:
                        output_file = merged_dir / media_file.name
                        if overlay_merge_single(media_file, overlay, output_file):
                            merged_files.add(media_file.name)
                            stats['total_merged'] += 1
                    except Exception as e:
                        logger.error(f"Error merging {media_file.name}: {e}")
                
                # Mark overlay as merged
                merged_files.add(overlay.name)
            else:
                # Grouped: different overlays - pair them individually
                logger.info(f"Processing grouped files for {date_str}: {len(media_files)} media with {len(overlay_files)} overlays")
                
                for media, overlay in zip(media_sorted, overlay_sorted):
                    try:
                        output_file = merged_dir / media.name
                        if overlay_merge_single(media, overlay, output_file):
                            merged_files.add(media.name)
                            merged_files.add(overlay.name)
                            stats['total_merged'] += 1
                    except Exception as e:
                        logger.error(f"Error merging {media.name}: {e}")

    logger.info(f"Total merged operations: {stats['total_merged']}")
    logger.info("=" * 60)

    return merged_files, stats

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

def index_media_files(source_dir: Path, merged_dir: Optional[Path] = None) -> Tuple[Dict[str, MediaFile], Dict]:
    """Create index of all media files from source and merged directories."""
    logger.info("=" * 60)
    logger.info("Starting MEDIA INDEXING phase")
    logger.info("=" * 60)

    media_index = {}
    stats = {'total_files': 0, 'extracted_ids': 0}

    # Index source files
    for item in source_dir.iterdir():
        if not item.is_file() or "thumbnail" in item.name.lower() or "_overlay~" in item.name:
            continue

        stats['total_files'] += 1
        media_id = extract_media_id(item.name)

        media_file = MediaFile(
            filename=item.name,
            source_path=item,
            media_id=media_id,
            timestamp=extract_mp4_timestamp(item) if item.suffix.lower() == '.mp4' else None
        )

        if media_id:
            media_index[media_id] = media_file
            stats['extracted_ids'] += 1

    # Index merged files - these take precedence over source files
    if merged_dir and merged_dir.exists():
        for item in merged_dir.iterdir():
            if item.is_file():
                stats['total_files'] += 1
                media_id = extract_media_id(item.name)

                media_file = MediaFile(
                    filename=item.name,
                    source_path=item,
                    media_id=media_id,
                    timestamp=extract_mp4_timestamp(item) if item.suffix.lower() == '.mp4' else None,
                    is_merged=True
                )

                if media_id:
                    media_index[media_id] = media_file  # Merged files take precedence
                    stats['extracted_ids'] += 1

    logger.info(f"Indexed {stats['total_files']} files, extracted {stats['extracted_ids']} IDs")
    logger.info("=" * 60)

    return media_index, stats

def extract_mp4_timestamp(mp4_path: Path) -> Optional[int]:
    """Extract creation timestamp from MP4 file."""
    try:
        with open(mp4_path, "rb") as f:
            while True:
                header = f.read(8)
                if not header:
                    return None

                size = struct.unpack('>I', header[0:4])[0]
                atom_type = header[4:8]

                if atom_type == b'moov':
                    mvhd = f.read(8)
                    if mvhd[4:8] == b'mvhd':
                        version = f.read(1)[0]
                        f.seek(3, 1)

                        if version == 0:
                            creation_time = struct.unpack('>I', f.read(4))[0]
                        else:
                            creation_time = struct.unpack('>Q', f.read(8))[0]

                        return (creation_time - QUICKTIME_EPOCH_ADJUSTER) * 1000
                    return None

                if size == 1:
                    f.seek(struct.unpack('>Q', f.read(8))[0] - 16, 1)
                else:
                    f.seek(size - 8, 1)
    except Exception:
        return None

def map_media_to_messages(conversations: Dict[str, List], media_index: Dict[str, MediaFile]) -> Tuple[Dict, Set[str], Dict]:
    """Map media files to conversation messages."""
    logger.info("=" * 60)
    logger.info("Starting MEDIA MAPPING phase")
    logger.info("=" * 60)

    mappings = defaultdict(dict)
    mapped_files = set()
    stats = {'mapped_by_id': 0, 'mapped_by_timestamp': 0, 'merged_mapped': 0}

    # Phase 1: Map by Media ID
    logger.info("Phase 1: Mapping by Media ID...")
    for conv_id, messages in conversations.items():
        for i, msg in enumerate(messages):
            media_ids_str = msg.get("Media IDs", "")
            if not media_ids_str:
                continue

            for media_id in media_ids_str.split('|'):
                media_id = media_id.strip()
                if media_id in media_index:
                    media_file = media_index[media_id]

                    if i not in mappings[conv_id]:
                        mappings[conv_id][i] = []

                    mappings[conv_id][i].append({
                        "media_file": media_file,
                        "mapping_method": "media_id"
                    })
                    mapped_files.add(media_file.filename)
                    stats['mapped_by_id'] += 1
                    
                    if media_file.is_merged:
                        stats['merged_mapped'] += 1
                        logger.debug(f"Mapped merged file {media_file.filename} to message via media_id {media_id}")
                else:
                    logger.debug(f"Media ID {media_id} not found in index")

    # Phase 2: Map unmapped files by timestamp
    logger.info("Phase 2: Mapping by timestamp...")

    # Build message timestamp index
    msg_timestamps = []
    for conv_id, messages in conversations.items():
        for i, msg in enumerate(messages):
            ts = int(msg.get("Created(microseconds)", 0))
            if ts > 0:
                msg_timestamps.append((conv_id, i, ts))
    msg_timestamps.sort(key=lambda x: x[2])

    # Map unmapped files with timestamps
    for media_id, media_file in media_index.items():
        if media_file.filename in mapped_files:
            continue


        if not media_file.timestamp:
            continue

        best_match = None
        min_diff = float('inf')

        for conv_id, msg_idx, msg_ts in msg_timestamps:
            diff = abs(media_file.timestamp - msg_ts)
            if diff < min_diff:
                min_diff = diff
                best_match = (conv_id, msg_idx)

        if best_match and min_diff <= TIMESTAMP_THRESHOLD_SECONDS * 1000:
            conv_id, msg_idx = best_match
            if msg_idx not in mappings[conv_id]:
                mappings[conv_id][msg_idx] = []

            mappings[conv_id][msg_idx].append({
                "media_file": media_file,
                "mapping_method": "timestamp",
                "time_diff_seconds": round(min_diff / 1000.0, 1)
            })
            mapped_files.add(media_file.filename)
            stats['mapped_by_timestamp'] += 1
            
            if media_file.is_merged:
                stats['merged_mapped'] += 1
                logger.debug(f"Mapped merged file {media_file.filename} to message via timestamp")

    logger.info(f"Mapped {stats['mapped_by_id']} by ID, {stats['mapped_by_timestamp']} by timestamp")
    logger.info("=" * 60)

    return mappings, mapped_files, stats