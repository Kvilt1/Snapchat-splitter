"""System capability detection for cross-platform ffmpeg optimization."""

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Tuple
import psutil

logger = logging.getLogger(__name__)

class SystemCapabilities:
    """Detect and cache system capabilities for optimal encoding."""
    
    def __init__(self):
        self.ffmpeg_path = None
        self.ffprobe_path = None
        self.has_nvenc = False
        self.has_qsv = False
        self.has_vaapi = False
        self.has_videotoolbox = False
        self.cpu_count = psutil.cpu_count(logical=True)
        self.memory_gb = psutil.virtual_memory().total / (1024**3)
        self.os_type = platform.system()  # 'Windows', 'Darwin', 'Linux'
        
        self._detect_ffmpeg()
        self._detect_gpu()
    
    def _detect_ffmpeg(self) -> None:
        """Detect ffmpeg and ffprobe binaries."""
        # Try to find ffmpeg
        self.ffmpeg_path = shutil.which('ffmpeg')
        self.ffprobe_path = shutil.which('ffprobe')
        
        if not self.ffmpeg_path:
            logger.error("ffmpeg not found in PATH! Please install ffmpeg.")
            logger.error("Visit: https://ffmpeg.org/download.html")
            raise RuntimeError("ffmpeg is required but not found in system PATH")
        
        # Get ffmpeg version
        try:
            result = subprocess.run(
                [self.ffmpeg_path, '-version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            version_line = result.stdout.split('\n')[0]
            logger.info(f"Found ffmpeg: {version_line}")
        except Exception as e:
            logger.warning(f"Could not get ffmpeg version: {e}")
    
    def _detect_gpu(self) -> None:
        """Detect available GPU encoders."""
        if not self.ffmpeg_path:
            return
        
        try:
            # Get list of encoders
            result = subprocess.run(
                [self.ffmpeg_path, '-hide_banner', '-encoders'],
                capture_output=True,
                text=True,
                timeout=5
            )
            encoders = result.stdout.lower()
            
            # Check for NVIDIA NVENC (Windows, Linux)
            if 'h264_nvenc' in encoders or 'hevc_nvenc' in encoders:
                self.has_nvenc = self._verify_nvenc()
                if self.has_nvenc:
                    logger.info("✓ NVIDIA NVENC hardware encoder available")
            
            # Check for Intel Quick Sync (Windows, Linux)
            if 'h264_qsv' in encoders:
                self.has_qsv = True
                logger.info("✓ Intel Quick Sync Video (QSV) available")
            
            # Check for VAAPI (Linux)
            if 'h264_vaapi' in encoders:
                self.has_vaapi = True
                logger.info("✓ VA-API hardware encoder available (Linux)")
            
            # Check for VideoToolbox (macOS)
            if 'h264_videotoolbox' in encoders:
                self.has_videotoolbox = True
                logger.info("✓ VideoToolbox hardware encoder available (macOS)")
            
            if not any([self.has_nvenc, self.has_qsv, self.has_vaapi, self.has_videotoolbox]):
                logger.warning("No hardware encoders detected - will use CPU encoding")
                logger.info(f"CPU cores available: {self.cpu_count}")
                
        except Exception as e:
            logger.warning(f"Could not detect GPU encoders: {e}")
    
    def _verify_nvenc(self) -> bool:
        """Verify NVENC is actually usable (not just listed)."""
        try:
            # Try nvidia-smi to verify NVIDIA GPU presence
            nvidia_smi = shutil.which('nvidia-smi')
            if nvidia_smi:
                result = subprocess.run(
                    [nvidia_smi, '--query-gpu=name,driver_version', '--format=csv,noheader'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    gpu_info = result.stdout.strip().split(',')
                    logger.info(f"NVIDIA GPU detected: {gpu_info[0].strip()}")
                    return True
        except Exception:
            pass
        
        # If nvidia-smi not available, assume NVENC works if listed
        return True
    
    def get_optimal_encoder(self) -> Tuple[str, Dict]:
        """
        Get optimal encoder and settings based on available hardware.
        
        Returns:
            (encoder_name, encoder_options)
        """
        # Priority: NVENC > VideoToolbox > QSV > VAAPI > CPU
        
        if self.has_nvenc:
            return 'h264_nvenc', {
                'vcodec': 'h264_nvenc',
                'preset': 'p4',  # p1-p7, p4 is balanced
                'cq': '23',      # Constant quality 0-51
                'b:v': '0',      # Let CQ control bitrate
            }
        
        if self.has_videotoolbox:
            return 'h264_videotoolbox', {
                'vcodec': 'h264_videotoolbox',
                'b:v': '5M',     # 5 Mbps bitrate
            }
        
        if self.has_qsv:
            return 'h264_qsv', {
                'vcodec': 'h264_qsv',
                'preset': 'medium',
                'global_quality': '23',
            }
        
        if self.has_vaapi:
            return 'h264_vaapi', {
                'vcodec': 'h264_vaapi',
                'qp': '23',
            }
        
        # Fallback to CPU encoding
        return 'libx264', {
            'vcodec': 'libx264',
            'preset': 'ultrafast',  # Fast encoding for CPU
            'crf': '23',
        }
    
    def get_optimal_workers(self, custom_workers: Optional[int] = None) -> int:
        """
        Calculate optimal number of parallel encoding workers.
        
        Args:
            custom_workers: User-specified worker count (overrides auto-detection)
        
        Returns:
            Optimal worker count
        """
        if custom_workers is not None and custom_workers > 0:
            logger.info(f"Using custom worker count: {custom_workers}")
            return custom_workers
        
        # GPU-based workers
        if self.has_nvenc:
            # NVIDIA: depends on GPU model and VRAM
            # Conservative estimate: 4-6 workers for most consumer GPUs
            # High-end cards (>8GB VRAM): 6-8 workers
            if self.memory_gb >= 24:  # System RAM as proxy for GPU capability
                return 8
            elif self.memory_gb >= 16:
                return 6
            else:
                return 4
        
        if self.has_videotoolbox:
            # macOS VideoToolbox: 4-6 workers is safe
            return 4
        
        if self.has_qsv or self.has_vaapi:
            # Intel QSV / VAAPI: 4-6 workers
            return 4
        
        # CPU encoding: use half of available cores (conservative)
        cpu_workers = max(1, self.cpu_count // 2)
        logger.info(f"Using CPU encoding with {cpu_workers} workers")
        return cpu_workers
    
    def get_capabilities_summary(self) -> str:
        """Get human-readable summary of system capabilities."""
        lines = [
            f"System: {self.os_type}",
            f"CPU cores: {self.cpu_count}",
            f"RAM: {self.memory_gb:.1f} GB",
            f"ffmpeg: {self.ffmpeg_path or 'NOT FOUND'}",
        ]
        
        encoders = []
        if self.has_nvenc:
            encoders.append("NVENC")
        if self.has_qsv:
            encoders.append("QSV")
        if self.has_vaapi:
            encoders.append("VAAPI")
        if self.has_videotoolbox:
            encoders.append("VideoToolbox")
        
        if encoders:
            lines.append(f"Hardware encoders: {', '.join(encoders)}")
        else:
            lines.append("Hardware encoders: None (using CPU)")
        
        encoder_name, _ = self.get_optimal_encoder()
        workers = self.get_optimal_workers()
        lines.append(f"Selected encoder: {encoder_name}")
        lines.append(f"Parallel workers: {workers}")
        
        return "\n".join(lines)


# Global singleton
_system_capabilities = None

def get_system_capabilities() -> SystemCapabilities:
    """Get or create system capabilities singleton."""
    global _system_capabilities
    if _system_capabilities is None:
        _system_capabilities = SystemCapabilities()
    return _system_capabilities

