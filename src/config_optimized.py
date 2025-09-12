"""Optimized configuration with performance tuning parameters."""

import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any
import psutil
import multiprocessing as mp

# Base Configuration
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
TIMESTAMP_THRESHOLD_SECONDS = 60
QUICKTIME_EPOCH_ADJUSTER = 2082844800

# Performance Configuration
PERFORMANCE_CONFIG = {
    # Memory limits
    'max_memory_mb': int(os.environ.get('MAX_MEMORY_MB', 2048)),
    'json_chunk_size_mb': int(os.environ.get('JSON_CHUNK_SIZE_MB', 10)),
    
    # Processing limits
    'max_workers': int(os.environ.get('MAX_WORKERS', max(1, mp.cpu_count() - 1))),
    'batch_size': int(os.environ.get('BATCH_SIZE', 100)),
    'db_batch_size': int(os.environ.get('DB_BATCH_SIZE', 1000)),
    
    # FFmpeg settings
    'ffmpeg_threads': int(os.environ.get('FFMPEG_THREADS', 2)),
    'ffmpeg_preset': os.environ.get('FFMPEG_PRESET', 'ultrafast'),
    'ffmpeg_crf': int(os.environ.get('FFMPEG_CRF', 23)),
    'ffmpeg_timeout': int(os.environ.get('FFMPEG_TIMEOUT', 120)),
    
    # File handling
    'use_mmap': os.environ.get('USE_MMAP', 'true').lower() == 'true',
    'use_database': os.environ.get('USE_DATABASE', 'true').lower() == 'true',
    'cleanup_temp': os.environ.get('CLEANUP_TEMP', 'true').lower() == 'true',
    
    # Progress tracking
    'show_progress': os.environ.get('SHOW_PROGRESS', 'true').lower() == 'true',
    'progress_interval': int(os.environ.get('PROGRESS_INTERVAL', 100)),
}

# Logging setup with performance monitoring
class PerformanceLogger:
    """Logger with performance monitoring capabilities."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.process = psutil.Process()
        self.last_memory_check = 0
        self.memory_check_interval = 10  # seconds
    
    def log_memory_usage(self):
        """Log current memory usage."""
        import time
        current_time = time.time()
        
        if current_time - self.last_memory_check > self.memory_check_interval:
            memory_info = self.process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            
            if memory_mb > PERFORMANCE_CONFIG['max_memory_mb']:
                self.logger.warning(f"Memory usage ({memory_mb:.1f}MB) exceeds limit ({PERFORMANCE_CONFIG['max_memory_mb']}MB)")
            
            self.last_memory_check = current_time
            return memory_mb
        return None
    
    def info(self, message):
        """Log info with optional memory check."""
        self.log_memory_usage()
        self.logger.info(message)
    
    def warning(self, message):
        """Log warning with memory check."""
        memory_mb = self.log_memory_usage()
        if memory_mb:
            message = f"{message} (Memory: {memory_mb:.1f}MB)"
        self.logger.warning(message)
    
    def error(self, message):
        """Log error with memory check."""
        memory_mb = self.log_memory_usage()
        if memory_mb:
            message = f"{message} (Memory: {memory_mb:.1f}MB)"
        self.logger.error(message)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s'
)

# Create performance logger instance
logger = PerformanceLogger()

def ensure_directory(path: Path) -> None:
    """Ensure directory exists."""
    path.mkdir(parents=True, exist_ok=True)

def load_json_optimized(path: Path, use_streaming: bool = True) -> Dict[str, Any]:
    """Load JSON file with optional streaming for large files."""
    if not path.exists():
        logger.error(f"JSON file not found: {path}")
        return {}
    
    file_size_mb = path.stat().st_size / 1024 / 1024
    
    # Use streaming for large files
    if use_streaming and file_size_mb > PERFORMANCE_CONFIG['json_chunk_size_mb']:
        logger.info(f"Loading large JSON file ({file_size_mb:.1f}MB) with streaming")
        
        try:
            import mmap
            with open(path, 'r+b') as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped_file:
                    data = mmapped_file.read().decode('utf-8')
                    return json.loads(data)
        except Exception as e:
            logger.error(f"Failed to load {path} with mmap: {e}")
            # Fallback to regular loading
    
    # Regular loading for small files
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return {}

def save_json_chunked(data: Dict[str, Any], path: Path, chunk_size: int = 1000) -> None:
    """Save dictionary to JSON file in chunks for large data."""
    ensure_directory(path.parent)
    
    # Check data size
    if isinstance(data, dict) and len(data) > chunk_size:
        # Use chunked writer for large data
        from json_streaming import ChunkedJSONWriter
        
        with ChunkedJSONWriter(path) as writer:
            for key, value in data.items():
                writer.write_item(key, value)
    else:
        # Regular save for small data
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def format_timestamp(timestamp_ms: int) -> str:
    """Convert millisecond timestamp to ISO format."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.isoformat().replace('+00:00', 'Z')

def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename."""
    import re
    return re.sub(r'[\\/*?:"<>|]', "", filename)[:255]

def get_memory_usage() -> float:
    """Get current memory usage in MB."""
    process = psutil.Process()
    return process.memory_info().rss / 1024 / 1024

def check_available_memory() -> float:
    """Check available system memory in MB."""
    memory = psutil.virtual_memory()
    return memory.available / 1024 / 1024

def should_use_batch_processing() -> bool:
    """Determine if batch processing should be used based on available memory."""
    available_mb = check_available_memory()
    return available_mb < PERFORMANCE_CONFIG['max_memory_mb'] * 2

class ProgressTracker:
    """Track and report processing progress."""
    
    def __init__(self, total: int, description: str = "Processing"):
        self.total = total
        self.current = 0
        self.description = description
        self.start_time = datetime.now()
        self.last_report = 0
    
    def update(self, count: int = 1):
        """Update progress."""
        self.current += count
        
        if PERFORMANCE_CONFIG['show_progress']:
            if self.current - self.last_report >= PERFORMANCE_CONFIG['progress_interval']:
                self.report()
                self.last_report = self.current
    
    def report(self):
        """Report current progress."""
        if self.total > 0:
            percentage = (self.current / self.total) * 100
            elapsed = (datetime.now() - self.start_time).total_seconds()
            
            if self.current > 0:
                rate = self.current / elapsed
                eta = (self.total - self.current) / rate if rate > 0 else 0
                
                logger.info(f"{self.description}: {self.current}/{self.total} ({percentage:.1f}%) "
                          f"Rate: {rate:.1f}/s ETA: {eta:.0f}s")
            else:
                logger.info(f"{self.description}: {self.current}/{self.total} ({percentage:.1f}%)")
    
    def finish(self):
        """Mark as finished and report final stats."""
        elapsed = (datetime.now() - self.start_time).total_seconds()
        rate = self.current / elapsed if elapsed > 0 else 0
        
        logger.info(f"{self.description} completed: {self.current} items in {elapsed:.1f}s "
                   f"(Rate: {rate:.1f}/s)")

# Export configuration
__all__ = [
    'INPUT_DIR', 'OUTPUT_DIR', 'TIMESTAMP_THRESHOLD_SECONDS', 
    'QUICKTIME_EPOCH_ADJUSTER', 'PERFORMANCE_CONFIG',
    'ensure_directory', 'load_json_optimized', 'save_json_chunked',
    'format_timestamp', 'sanitize_filename', 'logger',
    'get_memory_usage', 'check_available_memory', 
    'should_use_batch_processing', 'ProgressTracker'
]