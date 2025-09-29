"""Media processing service: overlay merging, indexing, and mapping."""

import hashlib
import logging
import os
import re
import struct
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# Direct ffmpeg-python import for overlay merging
import ffmpeg
# PIL for WebP to PNG conversion
from PIL import Image

from .config import (
    TIMESTAMP_THRESHOLD_SECONDS,
    QUICKTIME_EPOCH_ADJUSTER,
    CACHE_DIR,
    DEFAULT_MAX_WORKERS
)
from .data_models import MediaFile
from .utils import ensure_directory

logger = logging.getLogger(__name__)


class MediaService:
    """Service for handling media processing operations."""
    
    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS):
        """Initialize the media service."""
        self.max_workers = max_workers
        
    def merge_overlay_pairs(self, source_dir: Path, output_dir: Path) -> Tuple[Set[str], Dict[str, Any]]:
        """Find and merge media/overlay pairs using parallel processing."""
        logger.info("=" * 60)
        logger.info("Starting PARALLEL OVERLAY MERGING phase")
        logger.info("=" * 60)
        
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
                len(set(self._calculate_file_hash(f) for f in overlay_files)) == 1):
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
        overlay_files = list(set(op[1] for op in merge_operations))
        webp_conversion_map = self._batch_convert_webp_overlays(overlay_files, cache_dir)
        
        # Update merge operations to use PNG files where available
        updated_operations = []
        for media_file, overlay_file, output_file in merge_operations:
            if overlay_file in webp_conversion_map:
                updated_operations.append((media_file, webp_conversion_map[overlay_file], output_file))
                stats['webp_converted'] += 1
            else:
                updated_operations.append((media_file, overlay_file, output_file))
        
        merge_operations = updated_operations
        merged_files = set()
        
        # Execute operations in parallel with progress bar
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_op = {executor.submit(self._parallel_merge_worker, op): op for op in merge_operations}
            
            with tqdm(total=len(merge_operations), desc="Merging media with overlays", unit="files") as pbar:
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
    
    def index_media_files(self, source_dir: Path, merged_dir: Optional[Path] = None) -> Tuple[Dict[str, MediaFile], Dict]:
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
            media_id = self._extract_media_id(item.name)
            
            media_file = MediaFile(
                filename=item.name,
                source_path=item,
                media_id=media_id,
                timestamp=self._extract_mp4_timestamp(item) if item.suffix.lower() == '.mp4' else None
            )
            
            if media_id:
                media_index[media_id] = media_file
                stats['extracted_ids'] += 1
        
        # Index merged files - these take precedence over source files
        if merged_dir and merged_dir.exists():
            for item in merged_dir.iterdir():
                if item.is_file():
                    stats['total_files'] += 1
                    media_id = self._extract_media_id(item.name)
                    
                    media_file = MediaFile(
                        filename=item.name,
                        source_path=item,
                        media_id=media_id,
                        timestamp=self._extract_mp4_timestamp(item) if item.suffix.lower() == '.mp4' else None,
                        is_merged=True
                    )
                    
                    if media_id:
                        media_index[media_id] = media_file  # Merged files take precedence
                        stats['extracted_ids'] += 1
        
        logger.info(f"Indexed {stats['total_files']} files, extracted {stats['extracted_ids']} IDs")
        logger.info("=" * 60)
        
        return media_index, stats
    
    def map_media_to_messages(self, conversations: Dict[str, List], media_index: Dict[str, MediaFile]) -> Tuple[Dict, Set[str], Dict]:
        """Map media files to conversation messages."""
        logger.info("=" * 60)
        logger.info("Starting MEDIA MAPPING phase")
        logger.info("=" * 60)
        
        mappings = defaultdict(dict)
        mapped_files = set()
        stats = {'mapped_by_id': 0, 'mapped_by_timestamp': 0, 'fallback_snap_used': 0}
        
        # Phase 1: Map by Media ID
        logger.info("Phase 1: Mapping by Media ID...")
        self._map_by_media_id(conversations, media_index, mappings, mapped_files, stats)
        
        # Phase 2: Map unmapped files by timestamp
        logger.info("Phase 2: Mapping by timestamp...")
        self._map_by_timestamp(conversations, media_index, mappings, mapped_files, stats)
        
        logger.info(f"Mapped {stats['mapped_by_id']} by ID, {stats['mapped_by_timestamp']} by timestamp")
        if stats['fallback_snap_used'] > 0:
            logger.info(f"Used {stats['fallback_snap_used']} fallback mappings (2nd media on snap to prevent orphaning)")
        logger.info("=" * 60)
        
        return mappings, mapped_files, stats
    
    def _map_by_media_id(self, conversations: Dict[str, List], media_index: Dict[str, MediaFile],
                        mappings: defaultdict, mapped_files: Set[str], stats: Dict) -> None:
        """Map media files by Media ID."""
        for conv_id, messages in conversations.items():
            for i, msg in enumerate(messages):
                media_ids_str = msg.get("Media IDs", "")
                if not media_ids_str:
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
    
    def _map_by_timestamp(self, conversations: Dict[str, List], media_index: Dict[str, MediaFile],
                         mappings: defaultdict, mapped_files: Set[str], stats: Dict) -> None:
        """Map unmapped media files by timestamp."""
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
            if media_file.filename in mapped_files or not media_file.timestamp:
                continue
            
            # Find all potential matches within threshold
            potential_matches = []
            for conv_id, msg_idx, msg_ts in msg_timestamps:
                diff = abs(media_file.timestamp - msg_ts)
                if diff <= TIMESTAMP_THRESHOLD_SECONDS * 1000:
                    potential_matches.append((conv_id, msg_idx, msg_ts, diff))
            
            potential_matches.sort(key=lambda x: x[3])
            
            best_match = None
            min_diff = float('inf')
            fallback_match = None
            fallback_diff = float('inf')
            
            # Find the best available match (prioritize empty snaps)
            for conv_id, msg_idx, msg_ts, diff in potential_matches:
                if conv_id in conversations and msg_idx < len(conversations[conv_id]):
                    msg = conversations[conv_id][msg_idx]
                    msg_type = msg.get("Type", "")
                    
                    if msg_type == "snap":
                        if msg_idx in mappings[conv_id] and len(mappings[conv_id][msg_idx]) > 0:
                            if fallback_match is None or diff < fallback_diff:
                                fallback_match = (conv_id, msg_idx)
                                fallback_diff = diff
                            continue
                    
                    best_match = (conv_id, msg_idx)
                    min_diff = diff
                    break
            
            # Use fallback if no better match found
            if not best_match and fallback_match:
                conv_id, msg_idx = fallback_match
                if (conv_id in conversations and msg_idx < len(conversations[conv_id]) and
                    conversations[conv_id][msg_idx].get("Type") == "snap"):
                    best_match = fallback_match
                    min_diff = fallback_diff
                    logger.debug(f"Using locked snap as fallback for {media_file.filename} to prevent orphaning")
            
            if best_match and fallback_match and best_match == fallback_match:
                logger.debug(f"Media {media_file.filename} added to already-occupied snap to prevent orphaning")
                stats['fallback_snap_used'] += 1
            
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
    
    def _extract_media_id(self, filename: str) -> Optional[str]:
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
    
    def _extract_mp4_timestamp(self, mp4_path: Path) -> Optional[int]:
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
    
    def _calculate_file_hash(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of file."""
        try:
            with open(file_path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return None
    
    def _convert_webp_to_png(self, input_path: Path, output_path: Path) -> bool:
        """Convert a single WebP image to PNG efficiently."""
        try:
            if (output_path.exists() and 
                output_path.stat().st_mtime > input_path.stat().st_mtime):
                return True
            
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with Image.open(input_path) as img:
                if img.mode in ('RGBA', 'LA'):
                    img.save(output_path, 'PNG', optimize=True)
                else:
                    rgb_img = img.convert('RGB')
                    rgb_img.save(output_path, 'PNG', optimize=True)
            
            return True
        except Exception as e:
            logger.error(f"Failed to convert {input_path} to PNG: {e}")
            return False
    
    def _batch_convert_webp_worker(self, args: Tuple[Path, Path]) -> Optional[Tuple[Path, Path]]:
        """Worker function for parallel WebP to PNG conversion."""
        webp_path, png_path = args
        if self._convert_webp_to_png(webp_path, png_path):
            return (webp_path, png_path)
        return None
    
    def _batch_convert_webp_overlays(self, overlay_files: List[Path], cache_dir: Path) -> Dict[Path, Path]:
        """Convert multiple WebP overlay files to PNG in parallel."""
        webp_files = [f for f in overlay_files if f.suffix.lower() == '.webp']
        
        if not webp_files:
            return {}
        
        logger.info(f"Converting {len(webp_files)} WebP overlay files to PNG...")
        
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        conversion_ops = []
        path_mapping = {}
        
        for webp_path in webp_files:
            png_filename = webp_path.stem + '.png'
            png_path = cache_dir / png_filename
            conversion_ops.append((webp_path, png_path))
            path_mapping[webp_path] = png_path
        
        successful_conversions = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_op = {executor.submit(self._batch_convert_webp_worker, op): op for op in conversion_ops}
            
            with tqdm(total=len(conversion_ops), desc="Converting WebP overlays to PNG", unit="files") as pbar:
                for future in as_completed(future_to_op):
                    result = future.result()
                    if result:
                        webp_path, png_path = result
                        successful_conversions[webp_path] = png_path
                    pbar.update(1)
        
        logger.info(f"Successfully converted {len(successful_conversions)}/{len(webp_files)} WebP files to PNG")
        return successful_conversions
    
    def _run_ffmpeg_merge(self, media_file: Path, overlay_file: Path, output_path: Path, 
                         allow_overwriting: bool = True, quiet: bool = True) -> bool:
        """Merge media with overlay using direct ffmpeg-python."""
        try:
            vid = ffmpeg.input(str(media_file))
            overlay_img = ffmpeg.input(str(overlay_file))
            scaled = overlay_img.filter("scale", "-1", "rh")
            overlay_video = vid.overlay(scaled, eof_action="repeat")
            
            try:
                probe_result = ffmpeg.probe(str(media_file))
                has_audio = any(stream['codec_type'] == 'audio' for stream in probe_result['streams'])
            except:
                has_audio = False
            
            if has_audio:
                output_node = ffmpeg.output(
                    overlay_video, vid.audio, str(output_path),
                    vcodec="libx264", preset="ultrafast", crf=23, map_metadata=0,
                )
            else:
                output_node = ffmpeg.output(
                    overlay_video, str(output_path),
                    vcodec="libx264", preset="ultrafast", crf=23, map_metadata=0,
                )
            
            if allow_overwriting:
                output_node = output_node.overwrite_output()
            
            output_node.run(quiet=quiet)
            return True
            
        except ffmpeg.Error as err:
            stderr_output = err.stderr.decode('utf-8') if err.stderr else 'No stderr available'
            logger.error(f"ffmpeg error merging {media_file.name} with overlay {overlay_file.name}:")
            logger.error(f"  Error message: {err}")
            logger.error(f"  Stderr output: {stderr_output}")
            return False
        except Exception as e:
            logger.error(f"Error merging {media_file.name} with overlay {overlay_file.name}: {e}")
            return False
    
    def _overlay_merge_single(self, media_file: Path, overlay_file: Path, output_path: Path) -> bool:
        """Merge media with overlay using direct ffmpeg-python."""
        return self._run_ffmpeg_merge(media_file, overlay_file, output_path)
    
    def _parallel_merge_worker(self, args: Tuple[Path, Path, Path]) -> Optional[Tuple[str, str]]:
        """Worker function for parallel overlay merging."""
        media_file, overlay_file, output_file = args
        
        if (output_file.exists() and 
            output_file.stat().st_mtime > max(media_file.stat().st_mtime, overlay_file.stat().st_mtime)):
            return (media_file.name, overlay_file.name)
        
        if self._overlay_merge_single(media_file, overlay_file, output_file):
            return (media_file.name, overlay_file.name)
        return None


# Backward compatibility functions
def merge_overlay_pairs(source_dir: Path, output_dir: Path, max_workers: int = DEFAULT_MAX_WORKERS) -> Tuple[Set[str], Dict[str, Any]]:
    """Backward compatibility wrapper."""
    service = MediaService(max_workers)
    return service.merge_overlay_pairs(source_dir, output_dir)


def index_media_files(source_dir: Path, merged_dir: Optional[Path] = None) -> Tuple[Dict[str, MediaFile], Dict]:
    """Backward compatibility wrapper."""
    service = MediaService()
    return service.index_media_files(source_dir, merged_dir)


def map_media_to_messages(conversations: Dict[str, List], media_index: Dict[str, MediaFile]) -> Tuple[Dict, Set[str], Dict]:
    """Backward compatibility wrapper."""
    service = MediaService()
    return service.map_media_to_messages(conversations, media_index)


def cleanup_process_pool():
    """Cleanup function for compatibility with existing code."""
    # Clean up cache directory
    import shutil
    if CACHE_DIR.exists():
        try:
            shutil.rmtree(CACHE_DIR)
            logger.info(f"Cleaned up cache directory: {CACHE_DIR}")
        except Exception as e:
            logger.warning(f"Failed to clean up cache directory: {e}")


def extract_media_id(filename: str) -> Optional[str]:
    """Backward compatibility wrapper."""
    service = MediaService()
    return service._extract_media_id(filename)


def extract_mp4_timestamp(mp4_path: Path) -> Optional[int]:
    """Backward compatibility wrapper."""
    service = MediaService()
    return service._extract_mp4_timestamp(mp4_path)
