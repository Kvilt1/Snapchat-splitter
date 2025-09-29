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
    GPU_WORKERS,
    ensure_directory,
    MediaFile,
    Stats
)

# Direct ffmpeg-python import for overlay merging
import ffmpeg
# PIL for WebP to PNG conversion
from PIL import Image

logger = logging.getLogger(__name__)

# Cache directory for converted PNG files
CACHE_DIR = Path(".cache")

def convert_webp_to_png_optimized(input_path: Path, output_path: Path) -> bool:
    """
    Convert a single WebP image to PNG efficiently.
    
    Args:
        input_path: Path to input WebP file
        output_path: Path to output PNG file
        
    Returns:
        bool: True if conversion successful, False otherwise
    """
    try:
        # Skip if PNG already exists and is newer than WebP
        if (output_path.exists() and 
            output_path.stat().st_mtime > input_path.stat().st_mtime):
            return True
            
        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Convert WebP to PNG
        with Image.open(input_path) as img:
            # Convert to RGB if necessary (for transparency handling)
            if img.mode in ('RGBA', 'LA'):
                # Keep transparency
                img.save(output_path, 'PNG', optimize=True)
            else:
                # Convert to RGB for non-transparent images
                rgb_img = img.convert('RGB')
                rgb_img.save(output_path, 'PNG', optimize=True)
                
        return True
        
    except Exception as e:
        logger.error(f"Failed to convert {input_path} to PNG: {e}")
        return False

def batch_convert_webp_worker(args: Tuple[Path, Path]) -> Optional[Tuple[Path, Path]]:
    """Worker function for parallel WebP to PNG conversion."""
    webp_path, png_path = args
    if convert_webp_to_png_optimized(webp_path, png_path):
        return (webp_path, png_path)
    return None

def batch_convert_webp_overlays(overlay_files: List[Path], cache_dir: Path, max_workers: int = 8) -> Dict[Path, Path]:
    """
    Convert multiple WebP overlay files to PNG in parallel.
    
    Args:
        overlay_files: List of WebP overlay file paths
        cache_dir: Cache directory for converted PNG files
        max_workers: Maximum number of worker threads
        
    Returns:
        Dict mapping original WebP paths to converted PNG paths
    """
    # Filter only WebP files
    webp_files = [f for f in overlay_files if f.suffix.lower() == '.webp']
    
    if not webp_files:
        return {}
        
    logger.info(f"Converting {len(webp_files)} WebP overlay files to PNG...")
    
    # Ensure cache directory exists
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Prepare conversion operations
    conversion_ops = []
    path_mapping = {}
    
    for webp_path in webp_files:
        # Create PNG path in cache directory
        png_filename = webp_path.stem + '.png'
        png_path = cache_dir / png_filename
        conversion_ops.append((webp_path, png_path))
        path_mapping[webp_path] = png_path
    
    # Execute conversions in parallel with progress bar
    successful_conversions = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_op = {executor.submit(batch_convert_webp_worker, op): op for op in conversion_ops}
        
        # Progress bar for WebP conversion
        with tqdm(total=len(conversion_ops), desc="Converting WebP overlays", unit="files", 
                 bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]') as pbar:
            for future in as_completed(future_to_op):
                result = future.result()
                if result:
                    webp_path, png_path = result
                    successful_conversions[webp_path] = png_path
                pbar.update(1)
    
    logger.info(f"Successfully converted {len(successful_conversions)}/{len(webp_files)} WebP files to PNG")
    return successful_conversions

def cleanup_cache_directory():
    """Clean up cache directory to free disk space."""
    if CACHE_DIR.exists():
        try:
            shutil.rmtree(CACHE_DIR)
            logger.debug(f"Cleaned up cache directory: {CACHE_DIR}")
        except Exception as e:
            logger.debug(f"Failed to clean up cache directory: {e}")

def cleanup_process_pool():
    """Cleanup function - no longer needed since we use ThreadPoolExecutor with context managers."""
    # ThreadPoolExecutors are automatically cleaned up via 'with' statements
    # Cache cleanup is now handled by main.py's cleanup_temp_directories()
    pass

def run_ffmpeg_merge(media_file: Path, overlay_file: Path, output_path: Path, 
                     allow_overwriting: bool = True, quiet: bool = True, 
                     use_gpu: bool = True) -> bool:
    """
    Merge media with overlay using GPU acceleration.
    Returns True on success, False on failure.
    """
    try:
        vid = ffmpeg.input(str(media_file))
        overlay_img = ffmpeg.input(str(overlay_file))
        
        # Scale overlay to match video height
        scaled = overlay_img.filter("scale", "-1", "rh")
        overlay_video = vid.overlay(scaled, eof_action="repeat")
        
        # Check for audio stream
        try:
            probe_result = ffmpeg.probe(str(media_file))
            has_audio = any(stream['codec_type'] == 'audio' for stream in probe_result['streams'])
        except:
            has_audio = False
        
        # GPU-accelerated encoding settings
        if use_gpu:
            output_options = {
                'vcodec': 'h264_nvenc',           # NVIDIA hardware encoder
                'preset': 'fast',                  # Use standard preset (slow, medium, fast, hp, hq, etc.)
                'cq': '23',                        # Constant quality (0-51, lower = better)
                'b:v': '0',                        # Let CQ control bitrate
            }
        else:
            # Fallback to CPU encoding
            output_options = {
                'vcodec': 'libx264',
                'preset': 'ultrafast',
                'crf': '23',
            }
        
        output_options['map_metadata'] = 0
        
        # Create output with or without audio
        if has_audio:
            output_node = ffmpeg.output(overlay_video, vid.audio, str(output_path), **output_options)
        else:
            output_node = ffmpeg.output(overlay_video, str(output_path), **output_options)
        
        if allow_overwriting:
            output_node = output_node.overwrite_output()
        
        output_node.run(quiet=quiet)
        return True
        
    except ffmpeg.Error as err:
        # If GPU encoding fails, retry with CPU
        if use_gpu and 'nvenc' in str(err).lower():
            logger.warning(f"GPU encoding failed for {media_file.name}, falling back to CPU")
            return run_ffmpeg_merge(media_file, overlay_file, output_path, 
                                   allow_overwriting, quiet, use_gpu=False)
        
        stderr_output = err.stderr.decode('utf-8') if err.stderr else 'No stderr available'
        logger.error(f"ffmpeg error: {stderr_output}")
        return False
        
    except Exception as e:
        logger.error(f"Error merging {media_file.name}: {e}")
        return False
def overlay_merge_single(media_file: Path, overlay_file: Path, output_path: Path) -> bool:
    """
    Merge media with overlay using direct ffmpeg-python.
    Optimized for speed - skips timestamp preservation.
    """
    return run_ffmpeg_merge(media_file, overlay_file, output_path)

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


def get_optimal_gpu_workers() -> int:
    """
    Calculate optimal number of concurrent GPU encoding workers for GTX 1070.
    
    GTX 1070 Capabilities:
    - 2 NVENC hardware engines (can time-slice many more sessions)
    - 8GB VRAM (can handle 8-12 concurrent 1080p encodes with overlay)
    - GPU compute for overlay filter (adds load but not a bottleneck)
    
    Returns optimal worker count based primarily on GPU memory and NVENC capacity.
    CPU has minimal impact since encoding is GPU-bound.
    """
    # GTX 1070 optimal settings:
    # - Official NVENC limit: 2 concurrent (but NVIDIA removed this in drivers)
    # - Actual limit: ~8-12 sessions via time-slicing before performance degrades
    # - VRAM limit: 8GB can handle ~10-12 concurrent 1080p encodes
    # - Overlay filter: Adds ~500MB-1GB per stream
    # 
    # Optimal range for GTX 1070: 6-8 workers
    # - 6 workers: Conservative, ensures smooth encoding
    # - 8 workers: Aggressive, maximizes throughput
    # - 10+ workers: May cause VRAM pressure and encoding slowdown
    
    return 6  # Balanced for GTX 1070
    
    # Users can override by setting GPU_WORKERS in config.py:
    # - Set to 8 for maximum throughput (if VRAM allows)
    # - Set to 4 if you're running other GPU apps simultaneously
    # - Monitor with: watch -n 1 nvidia-smi


def parallel_merge_worker(args: Tuple[Path, Path, Path]) -> Optional[Tuple[str, str]]:
    """Worker function for parallel overlay merging."""
    media_file, overlay_file, output_file = args
    
    # Single-run optimization - no caching needed
    if overlay_merge_single(media_file, overlay_file, output_file):
        return (media_file.name, overlay_file.name)
    return None

def merge_overlay_pairs(source_dir: Path, output_dir: Path, max_workers: int = None) -> Tuple[Set[str], Dict[str, Any]]:
    """Find and merge media/overlay pairs using parallel processing."""
    logger.info("=" * 60)
    logger.info("Starting PARALLEL OVERLAY MERGING phase")
    logger.info("=" * 60)

    if max_workers is None:
        # Check config first, then auto-detect
        max_workers = GPU_WORKERS if GPU_WORKERS is not None else get_optimal_gpu_workers()
    
    logger.info(f"Using {max_workers} parallel workers for GPU-accelerated encoding")

    merged_dir = output_dir / "merged_media"
    ensure_directory(merged_dir)
    
    # Setup cache directory for WebP conversion
    cache_dir = CACHE_DIR / "converted_overlays"
    
    # Collect all merge operations
    merge_operations = []
    stats = {'total_media': 0, 'total_overlay': 0, 'total_merged': 0, 'webp_converted': 0}
    
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
    
    # Collect all merge operations from all groups
    for date_str, files in files_by_date.items():
        media_files = sorted(files["media"], key=lambda x: x.name)
        overlay_files = sorted(files["overlay"], key=lambda x: x.name)
        
        if not media_files or not overlay_files:
            continue
            
        if len(overlay_files) == 1 or (len(overlay_files) > 1 and 
            len(set(calculate_file_hash(f) for f in overlay_files)) == 1):
            # Single/multipart: use first overlay for all media
            overlay = overlay_files[0]
            for media in media_files:
                merge_operations.append((media, overlay, merged_dir / media.name))
        else:
            # Grouped: pair each media with its overlay
            for media, overlay in zip(media_files, overlay_files):
                merge_operations.append((media, overlay, merged_dir / media.name))
    
    logger.info(f"Found {len(merge_operations)} merge operations to process in parallel")
    
    # WEBP CONVERSION PHASE
    # Extract all unique overlay files that are WebP
    overlay_files = list(set(op[1] for op in merge_operations))
    webp_conversion_map = batch_convert_webp_overlays(overlay_files, cache_dir, max_workers)
    
    # Update merge operations to use PNG files where available
    updated_operations = []
    for media_file, overlay_file, output_file in merge_operations:
        if overlay_file in webp_conversion_map:
            # Use converted PNG instead of original WebP
            updated_operations.append((media_file, webp_conversion_map[overlay_file], output_file))
            stats['webp_converted'] += 1
        else:
            # Use original overlay file
            updated_operations.append((media_file, overlay_file, output_file))
    
    merge_operations = updated_operations
    merged_files = set()
    
    # Execute operations in parallel with progress bar
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_op = {executor.submit(parallel_merge_worker, op): op for op in merge_operations}
        
        # Progress bar for GPU-accelerated overlay merging
        with tqdm(total=len(merge_operations), desc="GPU encoding (NVENC)", unit="videos",
                 bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]') as pbar:
            for future in as_completed(future_to_op):
                result = future.result()
                if result:
                    media_name, overlay_name = result
                    merged_files.add(media_name)
                    merged_files.add(overlay_name)
                    stats['total_merged'] += 1
                pbar.update(1)

    logger.info(f"Completed {stats['total_merged']}/{len(merge_operations)} merge operations")
    if stats['webp_converted'] > 0:
        logger.info(f"Converted {stats['webp_converted']} WebP overlays to PNG for better compatibility")
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

    # Count total files first for progress bar
    source_files = [f for f in source_dir.iterdir() 
                   if f.is_file() and "thumbnail" not in f.name.lower() and "_overlay~" not in f.name]
    
    merged_files = []
    if merged_dir and merged_dir.exists():
        merged_files = [f for f in merged_dir.iterdir() if f.is_file()]
    
    total_files = len(source_files) + len(merged_files)
    
    # Index source files with progress bar (timestamps extracted lazily later)
    with tqdm(total=total_files, desc="Indexing media files", unit="files") as pbar:
        for item in source_files:
            stats['total_files'] += 1
            media_id = extract_media_id(item.name)

            media_file = MediaFile(
                filename=item.name,
                source_path=item,
                media_id=media_id,
                timestamp=None  # Extract lazily only when needed for timestamp mapping
            )

            if media_id:
                media_index[media_id] = media_file
                stats['extracted_ids'] += 1
            
            pbar.update(1)

        # Index merged files - these take precedence over source files
        for item in merged_files:
            stats['total_files'] += 1
            media_id = extract_media_id(item.name)

            media_file = MediaFile(
                filename=item.name,
                source_path=item,
                media_id=media_id,
                timestamp=None,  # Extract lazily only when needed for timestamp mapping
                is_merged=True
            )

            if media_id:
                media_index[media_id] = media_file  # Merged files take precedence
                stats['extracted_ids'] += 1
            
            pbar.update(1)

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


def extract_mp4_timestamp_fast(mp4_path: Path) -> Optional[int]:
    """
    Extract creation timestamp using ffprobe (faster than manual parsing).
    Falls back to manual extraction if ffprobe fails.
    """
    try:
        probe = ffmpeg.probe(str(mp4_path))
        
        # Try to get creation_time from format tags
        if 'format' in probe and 'tags' in probe['format']:
            creation_time = probe['format']['tags'].get('creation_time')
            if creation_time:
                # Parse ISO timestamp
                dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                return int(dt.timestamp() * 1000)
        
        # Try streams
        if 'streams' in probe:
            for stream in probe['streams']:
                if stream.get('codec_type') == 'video':
                    tags = stream.get('tags', {})
                    creation_time = tags.get('creation_time')
                    if creation_time:
                        dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                        return int(dt.timestamp() * 1000)
        
        # Fallback to manual extraction
        return extract_mp4_timestamp(mp4_path)
        
    except Exception:
        # Silent fallback to manual extraction
        return extract_mp4_timestamp(mp4_path)


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

def map_media_to_messages(conversations: Dict[str, List], media_index: Dict[str, MediaFile]) -> Tuple[Dict, Set[str], Dict]:
    """Map media files to conversation messages."""
    logger.info("=" * 60)
    logger.info("Starting MEDIA MAPPING phase")
    logger.info("=" * 60)

    mappings = defaultdict(dict)
    mapped_files = set()
    stats = {'mapped_by_id': 0, 'mapped_by_timestamp': 0, 'fallback_snap_used': 0}

    # Phase 1: Map by Media ID
    logger.info("Phase 1: Mapping by Media ID...")
    
    # Count total messages for progress tracking
    total_messages = sum(len(messages) for messages in conversations.values())
    
    with tqdm(total=total_messages, desc="Mapping by Media ID", unit="msgs") as pbar:
        for conv_id, messages in conversations.items():
            for i, msg in enumerate(messages):
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

                        if i not in mappings[conv_id]:
                            mappings[conv_id][i] = []

                        mappings[conv_id][i].append({
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
    
    # Count unmapped MP4 files (need timestamps)
    unmapped_mp4s = [mf for mf in media_index.values() 
                     if mf.filename not in mapped_files and mf.source_path.suffix.lower() == '.mp4']
    
    logger.info(f"Extracting timestamps from {len(unmapped_mp4s)} unmapped MP4 files...")
    
    # Extract timestamps in parallel for unmapped MP4s only
    def extract_timestamp_worker(media_file: MediaFile) -> Tuple[str, Optional[int]]:
        ts = extract_mp4_timestamp_fast(media_file.source_path)
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
                        if msg_idx in mappings[conv_id] and len(mappings[conv_id][msg_idx]) > 0:
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
                if msg_idx not in mappings[conv_id]:
                    mappings[conv_id][msg_idx] = []

                mappings[conv_id][msg_idx].append({
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

    return mappings, mapped_files, stats