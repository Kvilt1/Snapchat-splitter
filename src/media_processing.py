"""Media processing: overlay merging, indexing, and mapping."""

import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

from config import (
    TIMESTAMP_THRESHOLD_SECONDS,
    QUICKTIME_EPOCH_ADJUSTER,
    ensure_directory,
    MediaFile,
    Stats
)

logger = logging.getLogger(__name__)

# Global process pool for FFmpeg operations
_pool = None

def get_process_pool():
    """Get or create global process pool."""
    global _pool
    if _pool is None:
        _pool = Pool(processes=max(1, cpu_count() - 1))
    return _pool

def cleanup_process_pool():
    """Close and cleanup process pool."""
    global _pool
    if _pool:
        _pool.close()
        _pool.join()
        _pool = None

def _ffmpeg_worker(args: Tuple[Path, Path, Path]) -> Optional[Tuple[str, Optional[int]]]:
    """Worker function for FFmpeg merging."""
    media_file, overlay_file, output_path = args
    if run_ffmpeg_merge(media_file, overlay_file, output_path):
        timestamp = extract_mp4_timestamp(media_file)
        return (media_file.name, timestamp)
    return None

def run_ffmpeg_merge(media_file: Path, overlay_file: Path, output_file: Path) -> bool:
    """Merge media with overlay using FFmpeg."""
    if not shutil.which("ffmpeg"):
        return False

    ensure_directory(output_file.parent)

    try:
        command = [
            "ffmpeg", "-y",
            "-i", str(media_file),
            "-i", str(overlay_file),
            "-filter_complex",
            "[1:v][0:v]scale=w=rw:h=rh,format=rgba[ovr];[0:v][ovr]overlay=0:0:format=auto[vout]",
            "-map", "[vout]",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-movflags", "+faststart",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-c:a", "copy",
            str(output_file)
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)

        if result.returncode == 0:
            # Preserve timestamps
            st = media_file.stat()
            os.utime(output_file, (st.st_atime, st.st_mtime))
            return True
    except Exception as e:
        logger.error(f"FFmpeg error for {media_file.name}: {e}")
    return False

def calculate_file_hash(file_path: Path) -> Optional[str]:
    """Calculate MD5 hash of file."""
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None

def process_media_group(media_files: List[Path], overlay_files: List[Path],
                       output_dir: Path, group_type: str) -> Tuple[Optional[str], Dict]:
    """Process a group of media files with overlays."""
    stats = {'attempted': len(media_files), 'successful': 0, 'failed': 0}

    media_sorted = sorted(media_files, key=lambda x: x.name)
    overlay_sorted = sorted(overlay_files, key=lambda x: x.name) if len(overlay_files) > 1 else overlay_files

    folder_name = f"{media_sorted[-1].stem}_{group_type}"
    folder_path = output_dir / folder_name

    try:
        ensure_directory(folder_path)

        # Build task list
        tasks = []
        if len(overlay_files) == 1:
            # Single overlay for all media
            for media in media_sorted:
                tasks.append((media, overlay_files[0], folder_path / media.name))
        else:
            # Paired overlays
            for media, overlay in zip(media_sorted, overlay_sorted):
                tasks.append((media, overlay, folder_path / media.name))

        logger.info(f"Processing {group_type} group: {folder_name} with {len(tasks)} files")

        timestamps = {}
        pool = get_process_pool()
        results = pool.map(_ffmpeg_worker, tasks)

        for result in results:
            if result:
                stats['successful'] += 1
                filename, timestamp = result
                if timestamp:
                    dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                    timestamps[filename] = dt.isoformat().replace('+00:00', 'Z')
            else:
                stats['failed'] += 1

        if stats['successful'] > 0:
            with open(folder_path / "timestamps.json", 'w', encoding='utf-8') as f:
                json.dump(timestamps, f, indent=2)
            return folder_name, stats
        else:
            if folder_path.exists():
                shutil.rmtree(folder_path)
    except Exception as e:
        logger.error(f"Error processing {group_type} group: {e}")
        if folder_path.exists():
            shutil.rmtree(folder_path)

    return None, stats

def merge_overlay_pairs(source_dir: Path, output_dir: Path) -> Tuple[Set[str], Dict[str, Any]]:
    """Find and merge media/overlay pairs directly to output."""
    logger.info("=" * 60)
    logger.info("Starting OVERLAY MERGING phase")
    logger.info("=" * 60)

    stats = {
        'total_media': 0,
        'total_overlay': 0,
        'total_merged': 0
    }

    if not shutil.which("ffmpeg"):
        logger.warning("FFmpeg not found. Skipping overlay merging.")
        return set(), stats

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
    pool = get_process_pool()

    for date_str, files in files_by_date.items():
        media_files = files["media"]
        overlay_files = files["overlay"]

        if len(media_files) == 1 and len(overlay_files) == 1:
            # Simple pair
            output_file = merged_dir / media_files[0].name
            if run_ffmpeg_merge(media_files[0], overlay_files[0], output_file):
                merged_files.add(media_files[0].name)
                merged_files.add(overlay_files[0].name)
                stats['total_merged'] += 1

        elif len(overlay_files) > 0 and len(media_files) > 0:
            # Check if overlays are identical (multipart) or different (grouped)
            if len(overlay_files) == 1 or (len(overlay_files) > 1 and
                len(set(calculate_file_hash(f) for f in overlay_files)) == 1):
                # Multipart: same overlay for all
                folder_name, group_stats = process_media_group(
                    media_files, overlay_files[:1], merged_dir, "multipart"
                )
            else:
                # Grouped: different overlays
                folder_name, group_stats = process_media_group(
                    media_files, overlay_files, merged_dir, "grouped"
                )

            if folder_name:
                for f in media_files + overlay_files:
                    merged_files.add(f.name)
                stats['total_merged'] += group_stats['successful']

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

    # Index merged files
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
                    media_index[media_id] = media_file
                    stats['extracted_ids'] += 1

            elif item.is_dir() and (item.name.endswith("_multipart") or item.name.endswith("_grouped")):
                # Index grouped folders - create one MediaFile for the folder
                # Map all media IDs in the folder to the same MediaFile
                media_file = MediaFile(
                    filename=item.name,  # Use folder name
                    source_path=item,     # Point to folder
                    media_id=None,  # Will be set from first file
                    is_merged=True,
                    is_grouped=True
                )

                folder_indexed = False
                for file_path in item.iterdir():
                    if file_path.suffix.lower() == '.mp4':
                        media_id = extract_media_id(file_path.name)

                        if media_id:
                            if not folder_indexed:
                                stats['total_files'] += 1
                                media_file.media_id = media_id  # Set primary media ID
                                folder_indexed = True
                                logger.debug(f"Indexed grouped folder: {item.name} with primary ID {media_id}")

                            # Map this media ID to the folder
                            media_index[media_id] = media_file
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
    stats = {'mapped_by_id': 0, 'mapped_by_timestamp': 0}

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

        # For grouped folders, read the earliest timestamp from timestamps.json
        if media_file.is_grouped:
            timestamps_file = media_file.source_path / "timestamps.json"
            if timestamps_file.exists():
                import json
                from datetime import datetime
                with open(timestamps_file) as f:
                    timestamps_data = json.load(f)
                    # Find the earliest timestamp
                    min_timestamp = None
                    for iso_ts in timestamps_data.values():
                        dt = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
                        ts_ms = int(dt.timestamp() * 1000)
                        if min_timestamp is None or ts_ms < min_timestamp:
                            min_timestamp = ts_ms
                    media_file.timestamp = min_timestamp

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

            # Check if this is a duplicate grouped folder mapping
            already_mapped = False
            if media_file.is_grouped:
                for existing_item in mappings[conv_id][msg_idx]:
                    if existing_item["media_file"].filename == media_file.filename:
                        already_mapped = True
                        break

            if not already_mapped:
                mappings[conv_id][msg_idx].append({
                    "media_file": media_file,
                    "mapping_method": "timestamp",
                    "time_diff_seconds": round(min_diff / 1000.0, 1)
                })
                mapped_files.add(media_file.filename)
                stats['mapped_by_timestamp'] += 1

    logger.info(f"Mapped {stats['mapped_by_id']} by ID, {stats['mapped_by_timestamp']} by timestamp")
    logger.info("=" * 60)

    return mappings, mapped_files, stats