"""Optimized media processing with parallel processing and memory efficiency."""

import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, Iterator
import multiprocessing as mp
from functools import partial
import tempfile

from config import (
    TIMESTAMP_THRESHOLD_SECONDS,
    QUICKTIME_EPOCH_ADJUSTER,
    ensure_directory,
    format_timestamp
)

logger = logging.getLogger(__name__)


class OptimizedMediaProcessor:
    """Memory-efficient media processor with parallel processing."""
    
    def __init__(self, max_workers: Optional[int] = None, 
                 batch_size: int = 100,
                 max_memory_mb: int = 1024):
        """
        Initialize optimized media processor.
        
        Args:
            max_workers: Maximum parallel workers (defaults to CPU count)
            batch_size: Size of batches for processing
            max_memory_mb: Maximum memory usage in MB
        """
        self.max_workers = max_workers or max(1, mp.cpu_count() - 1)
        self.batch_size = batch_size
        self.max_memory_mb = max_memory_mb
        self.temp_dir = None
    
    def process_media_directory_generator(self, source_dir: Path) -> Iterator[Path]:
        """
        Generator to iterate through media files without loading all into memory.
        """
        for file_path in source_dir.iterdir():
            if file_path.is_file():
                # Skip thumbnails and stubs
                name_lower = file_path.name.lower()
                if "thumbnail" not in name_lower and "media~zip-" not in file_path.name:
                    yield file_path
    
    def calculate_file_hash_chunked(self, file_path: Path, chunk_size: int = 8192) -> Optional[str]:
        """Calculate MD5 hash of file using chunked reading."""
        try:
            md5_hash = hashlib.md5()
            with open(file_path, 'rb') as f:
                while chunk := f.read(chunk_size):
                    md5_hash.update(chunk)
            return md5_hash.hexdigest()
        except Exception:
            return None
    
    def run_ffmpeg_merge_optimized(self, media_file: Path, overlay_file: Path, 
                                  output_file: Path) -> bool:
        """Optimized FFmpeg merge with better presets and resource limits."""
        if not shutil.which("ffmpeg"):
            return False
        
        try:
            # Use ultrafast preset and limit threads for better parallelization
            command = [
                "ffmpeg", "-y",
                "-threads", "2",  # Limit threads per process
                "-i", str(media_file),
                "-i", str(overlay_file),
                "-filter_complex",
                "[1:v][0:v]scale=w=rw:h=rh,format=rgba[ovr];[0:v][ovr]overlay=0:0:format=auto[vout]",
                "-map", "[vout]",
                "-map", "0:a?",
                "-map_metadata", "0",
                "-movflags", "+faststart",
                "-c:v", "libx264",
                "-preset", "ultrafast",  # Fastest encoding
                "-crf", "23",  # Slightly lower quality for speed
                "-c:a", "copy",
                str(output_file)
            ]
            
            # Run with timeout and resource limits
            result = subprocess.run(
                command, 
                capture_output=True, 
                text=True, 
                timeout=120,  # 2 minute timeout
                env={**os.environ, 'FFMPEG_THREAD_QUEUE_SIZE': '512'}
            )
            
            if result.returncode == 0:
                # Preserve timestamps
                st = media_file.stat()
                os.utime(output_file, (st.st_atime, st.st_mtime))
                return True
                
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg timeout for {media_file.name}")
        except Exception as e:
            logger.error(f"FFmpeg error for {media_file.name}: {e}")
        
        return False
    
    def merge_overlay_pairs_optimized(self, source_dir: Path, temp_dir: Path) -> Tuple[Set[str], Dict[str, Any]]:
        """Optimized overlay merging with parallel processing and batching."""
        logger.info("=" * 60)
        logger.info("Starting OPTIMIZED OVERLAY MERGING phase")
        logger.info("=" * 60)
        
        stats = {
            'total_media': 0,
            'total_overlay': 0,
            'simple_pairs_attempted': 0,
            'simple_pairs_succeeded': 0,
            'multipart_attempted': 0,
            'multipart_succeeded': 0,
            'grouped_attempted': 0,
            'grouped_succeeded': 0,
            'ffmpeg_errors': [],
            'total_merged': 0
        }
        
        if not shutil.which("ffmpeg"):
            logger.warning("FFmpeg not found. Skipping overlay merging.")
            return set(), stats
        
        ensure_directory(temp_dir)
        
        # Group files by date using generator for memory efficiency
        files_by_date = defaultdict(lambda: {"media": [], "overlay": []})
        
        for file_path in self.process_media_directory_generator(source_dir):
            match = re.match(r"(\d{4}-\d{2}-\d{2})", file_path.name)
            if not match:
                continue
            
            date_str = match.group(1)
            
            if "_media~" in file_path.name:
                files_by_date[date_str]["media"].append(file_path)
                stats['total_media'] += 1
            elif "_overlay~" in file_path.name:
                files_by_date[date_str]["overlay"].append(file_path)
                stats['total_overlay'] += 1
        
        logger.info(f"Found {stats['total_media']} media files and {stats['total_overlay']} overlay files")
        
        merged_files = set()
        
        # Process in parallel batches
        merge_tasks = []
        
        for date_str, files in files_by_date.items():
            media_files = files["media"]
            overlay_files = files["overlay"]
            
            # Simple pair
            if len(media_files) == 1 and len(overlay_files) == 1:
                merge_tasks.append({
                    'type': 'simple',
                    'media': media_files[0],
                    'overlay': overlay_files[0],
                    'output': temp_dir / media_files[0].name,
                    'date': date_str
                })
                stats['simple_pairs_attempted'] += 1
            
            # Multi-part or grouped
            elif len(overlay_files) > 1 and len(media_files) >= 1:
                # Check if overlays identical using hash comparison
                overlay_hashes = []
                for overlay in overlay_files[:3]:  # Sample first 3 for efficiency
                    hash_val = self.calculate_file_hash_chunked(overlay)
                    if hash_val:
                        overlay_hashes.append(hash_val)
                
                if len(set(overlay_hashes)) == 1:  # All same
                    merge_tasks.append({
                        'type': 'multipart',
                        'media_files': media_files,
                        'overlay': overlay_files[0],
                        'date': date_str,
                        'temp_dir': temp_dir
                    })
                    stats['multipart_attempted'] += 1
                else:
                    merge_tasks.append({
                        'type': 'grouped',
                        'media_files': media_files,
                        'overlay_files': overlay_files,
                        'date': date_str,
                        'temp_dir': temp_dir
                    })
                    stats['grouped_attempted'] += 1
        
        # Process merge tasks in parallel batches
        logger.info(f"Processing {len(merge_tasks)} merge tasks with {self.max_workers} workers")
        
        with ProcessPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit tasks in batches to control memory usage
            for i in range(0, len(merge_tasks), self.batch_size):
                batch = merge_tasks[i:i + self.batch_size]
                futures = []
                
                for task in batch:
                    if task['type'] == 'simple':
                        future = executor.submit(
                            self._process_simple_merge,
                            task['media'], task['overlay'], task['output']
                        )
                        futures.append((future, task))
                    elif task['type'] == 'multipart':
                        future = executor.submit(
                            self._process_multipart_batch,
                            task['date'], task['media_files'], 
                            task['overlay'], task['temp_dir']
                        )
                        futures.append((future, task))
                    elif task['type'] == 'grouped':
                        future = executor.submit(
                            self._process_grouped_batch,
                            task['date'], task['media_files'],
                            task['overlay_files'], task['temp_dir']
                        )
                        futures.append((future, task))
                
                # Process results as they complete
                for future, task in futures:
                    try:
                        result = future.result(timeout=300)  # 5 minute timeout
                        
                        if task['type'] == 'simple':
                            if result:
                                merged_files.add(task['media'].name)
                                merged_files.add(task['overlay'].name)
                                stats['simple_pairs_succeeded'] += 1
                                stats['total_merged'] += 1
                        elif task['type'] == 'multipart':
                            if result:
                                folder_name, files_merged = result
                                merged_files.update(files_merged)
                                stats['multipart_succeeded'] += 1
                                stats['total_merged'] += len(files_merged) // 2
                        elif task['type'] == 'grouped':
                            if result:
                                files_merged = result
                                merged_files.update(files_merged)
                                stats['grouped_succeeded'] += 1
                                stats['total_merged'] += len(files_merged) // 2
                    
                    except Exception as e:
                        logger.error(f"Error processing merge task: {e}")
                        stats['ffmpeg_errors'].append(str(e))
        
        # Log statistics
        logger.info("=" * 60)
        logger.info("OPTIMIZED OVERLAY MERGING RESULTS:")
        
        if stats['simple_pairs_attempted'] > 0:
            pct = (stats['simple_pairs_succeeded'] / stats['simple_pairs_attempted']) * 100
            logger.info(f"  Simple pairs: [{stats['simple_pairs_succeeded']}]/[{stats['simple_pairs_attempted']}] ({pct:.1f}%)")
        
        if stats['multipart_attempted'] > 0:
            pct = (stats['multipart_succeeded'] / stats['multipart_attempted']) * 100
            logger.info(f"  Multipart folders: [{stats['multipart_succeeded']}]/[{stats['multipart_attempted']}] ({pct:.1f}%)")
        
        if stats['grouped_attempted'] > 0:
            pct = (stats['grouped_succeeded'] / stats['grouped_attempted']) * 100
            logger.info(f"  Grouped folders: [{stats['grouped_succeeded']}]/[{stats['grouped_attempted']}] ({pct:.1f}%)")
        
        logger.info(f"  Total merged operations: {stats['total_merged']}")
        logger.info("=" * 60)
        
        return merged_files, stats
    
    def _process_simple_merge(self, media_file: Path, overlay_file: Path, 
                            output_file: Path) -> bool:
        """Process a simple media/overlay pair."""
        return self.run_ffmpeg_merge_optimized(media_file, overlay_file, output_file)
    
    def _process_multipart_batch(self, date_str: str, media_files: List[Path],
                                overlay_file: Path, temp_dir: Path) -> Optional[Tuple[str, Set[str]]]:
        """Process multipart files in batch."""
        media_files_sorted = sorted(media_files, key=lambda x: x.name)
        folder_name = media_files_sorted[-1].stem + "_multipart"
        folder_path = temp_dir / folder_name
        
        try:
            ensure_directory(folder_path)
            merged_files = set()
            timestamps = {}
            
            for media_file in media_files_sorted:
                output = folder_path / media_file.name
                if self.run_ffmpeg_merge_optimized(media_file, overlay_file, output):
                    merged_files.add(media_file.name)
                    merged_files.add(overlay_file.name)
                    
                    # Extract timestamp
                    timestamp = self.extract_mp4_timestamp(media_file)
                    if timestamp:
                        timestamps[media_file.name] = format_timestamp(timestamp)
            
            if merged_files:
                # Save timestamps
                with open(folder_path / "timestamps.json", 'w', encoding='utf-8') as f:
                    json.dump(timestamps, f, indent=2)
                return folder_name, merged_files
            else:
                # Cleanup on failure
                if folder_path.exists():
                    shutil.rmtree(folder_path)
                    
        except Exception as e:
            logger.error(f"Error processing multipart for {date_str}: {e}")
            if folder_path.exists():
                shutil.rmtree(folder_path)
        
        return None
    
    def _process_grouped_batch(self, date_str: str, media_files: List[Path],
                              overlay_files: List[Path], temp_dir: Path) -> Set[str]:
        """Process grouped files in batch."""
        merged = set()
        
        # Sort files for consistent pairing
        media_sorted = sorted(media_files, key=lambda x: x.name)
        overlay_sorted = sorted(overlay_files, key=lambda x: x.name)
        
        # Try to match by count
        if len(media_sorted) == len(overlay_sorted):
            folder_name = media_sorted[-1].stem + "_grouped"
            folder_path = temp_dir / folder_name
            
            try:
                ensure_directory(folder_path)
                timestamps = {}
                
                for media, overlay in zip(media_sorted, overlay_sorted):
                    output = folder_path / media.name
                    if self.run_ffmpeg_merge_optimized(media, overlay, output):
                        merged.add(media.name)
                        merged.add(overlay.name)
                        
                        # Extract timestamp
                        timestamp = self.extract_mp4_timestamp(media)
                        if timestamp:
                            timestamps[media.name] = format_timestamp(timestamp)
                
                if merged:
                    # Save timestamps
                    with open(folder_path / "timestamps.json", 'w', encoding='utf-8') as f:
                        json.dump(timestamps, f, indent=2)
                else:
                    # Cleanup on failure
                    if folder_path.exists():
                        shutil.rmtree(folder_path)
                        
            except Exception as e:
                logger.error(f"Error creating grouped folder: {e}")
                if folder_path.exists():
                    shutil.rmtree(folder_path)
        
        return merged
    
    def extract_mp4_timestamp(self, mp4_path: Path) -> Optional[int]:
        """Extract creation timestamp from MP4 file efficiently."""
        try:
            with open(mp4_path, "rb") as f:
                # Use buffered reading for efficiency
                buffer_size = 4096
                position = 0
                
                while True:
                    f.seek(position)
                    header = f.read(8)
                    if not header or len(header) < 8:
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
                    
                    # Move to next atom
                    if size == 1:
                        f.seek(position + 8)
                        extended_size = struct.unpack('>Q', f.read(8))[0]
                        position += extended_size
                    else:
                        position += size
                        
        except Exception:
            return None
    
    def index_media_files_optimized(self, media_dir: Path, db=None) -> Tuple[Dict[str, str], Dict[str, int]]:
        """Create optimized index of media files using database."""
        logger.info("=" * 60)
        logger.info("Starting OPTIMIZED MEDIA INDEXING phase")
        logger.info("=" * 60)
        
        stats = {
            'total_files': 0,
            'regular_files': 0,
            'multipart_folders': 0,
            'grouped_folders': 0,
            'extracted_ids': 0,
            'no_id_files': []
        }
        
        if not media_dir.exists():
            logger.warning(f"Media directory {media_dir} does not exist")
            return {}, stats
        
        media_index = {}
        media_items_batch = []
        
        # Process files in parallel batches
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = []
            
            for item in media_dir.iterdir():
                if item.is_file():
                    future = executor.submit(self._process_media_file, item)
                    futures.append(future)
                elif item.is_dir() and (item.name.endswith("_multipart") or item.name.endswith("_grouped")):
                    future = executor.submit(self._process_media_folder, item)
                    futures.append(future)
            
            # Process results
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        file_type, media_id, filename, file_path, is_grouped, timestamp, size = result
                        
                        if file_type == 'file':
                            stats['regular_files'] += 1
                        elif file_type == 'multipart':
                            stats['multipart_folders'] += 1
                        elif file_type == 'grouped':
                            stats['grouped_folders'] += 1
                        
                        stats['total_files'] += 1
                        
                        if media_id:
                            media_index[media_id] = filename
                            stats['extracted_ids'] += 1
                            
                            # Add to batch for database insertion
                            if db:
                                media_items_batch.append((
                                    media_id, filename, str(file_path), is_grouped,
                                    timestamp, size, None, None
                                ))
                        else:
                            stats['no_id_files'].append(filename)
                        
                        # Insert batch into database when it reaches batch size
                        if db and len(media_items_batch) >= self.batch_size:
                            db.insert_media_index_batch(media_items_batch)
                            media_items_batch = []
                            
                except Exception as e:
                    logger.error(f"Error processing media item: {e}")
        
        # Insert remaining items
        if db and media_items_batch:
            db.insert_media_index_batch(media_items_batch)
        
        # Log statistics
        logger.info("OPTIMIZED MEDIA INDEXING RESULTS:")
        logger.info(f"  Total files processed: {stats['total_files']}")
        logger.info(f"    - Regular files: {stats['regular_files']}")
        logger.info(f"    - Multipart folders: {stats['multipart_folders']}")
        logger.info(f"    - Grouped folders: {stats['grouped_folders']}")
        
        if stats['total_files'] > 0:
            success_pct = (stats['extracted_ids'] / stats['total_files']) * 100
            logger.info(f"  Media ID extraction: [{stats['extracted_ids']}]/[{stats['total_files']}] ({success_pct:.1f}%)")
        
        logger.info("=" * 60)
        
        return media_index, stats
    
    def _process_media_file(self, file_path: Path) -> Optional[Tuple]:
        """Process a single media file."""
        media_id = self.extract_media_id(file_path.name)
        timestamp = None
        
        if file_path.suffix.lower() == '.mp4':
            timestamp = self.extract_mp4_timestamp(file_path)
        
        size = file_path.stat().st_size
        
        return ('file', media_id, file_path.name, file_path, False, timestamp, size)
    
    def _process_media_folder(self, folder_path: Path) -> Optional[Tuple]:
        """Process a media folder (multipart or grouped)."""
        folder_type = 'multipart' if folder_path.name.endswith('_multipart') else 'grouped'
        
        # Get first MP4 file for media ID
        for file_path in folder_path.iterdir():
            if file_path.suffix.lower() == '.mp4':
                media_id = self.extract_media_id(file_path.name)
                timestamp = self.extract_mp4_timestamp(file_path)
                size = sum(f.stat().st_size for f in folder_path.iterdir() if f.is_file())
                
                return (folder_type, media_id, folder_path.name, folder_path, True, timestamp, size)
        
        return None
    
    def extract_media_id(self, filename: str) -> Optional[str]:
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