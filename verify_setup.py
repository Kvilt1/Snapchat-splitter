#!/usr/bin/env python3
"""Verify system setup for Snapchat Media Mapper."""

import sys
import platform
from pathlib import Path

def check_python_version():
    """Check Python version."""
    print("Checking Python version...")
    version = sys.version_info
    if version >= (3, 8):
        print(f"  ✓ Python {version.major}.{version.minor}.{version.micro} (OK)")
        return True
    else:
        print(f"  ✗ Python {version.major}.{version.minor}.{version.micro} (Need 3.8+)")
        return False

def check_dependencies():
    """Check required Python packages."""
    print("\nChecking Python dependencies...")
    missing = []
    
    packages = {
        'ffmpeg': 'ffmpeg-python',
        'PIL': 'Pillow',
        'numpy': 'numpy',
        'tqdm': 'tqdm',
        'psutil': 'psutil',
        'pytz': 'pytz',
        'imageio': 'imageio'
    }
    
    for import_name, package_name in packages.items():
        try:
            __import__(import_name)
            print(f"  ✓ {package_name}")
        except ImportError:
            print(f"  ✗ {package_name} (missing)")
            missing.append(package_name)
    
    if missing:
        print(f"\nTo install missing packages:")
        print(f"  pip install {' '.join(missing)}")
        return False
    return True

def check_ffmpeg():
    """Check if ffmpeg is installed."""
    print("\nChecking ffmpeg installation...")
    import shutil
    import subprocess
    
    ffmpeg_path = shutil.which('ffmpeg')
    ffprobe_path = shutil.which('ffprobe')
    
    if not ffmpeg_path:
        print("  ✗ ffmpeg not found in PATH")
        print("\nPlease install ffmpeg:")
        sys_name = platform.system()
        if sys_name == 'Windows':
            print("  Windows: choco install ffmpeg")
        elif sys_name == 'Darwin':
            print("  macOS: brew install ffmpeg")
        elif sys_name == 'Linux':
            print("  Linux: sudo apt install ffmpeg  (or your distro's package manager)")
        return False
    
    print(f"  ✓ ffmpeg found: {ffmpeg_path}")
    
    if not ffprobe_path:
        print("  ⚠ ffprobe not found (usually comes with ffmpeg)")
    else:
        print(f"  ✓ ffprobe found: {ffprobe_path}")
    
    # Get version
    try:
        result = subprocess.run(
            [ffmpeg_path, '-version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        version_line = result.stdout.split('\n')[0]
        print(f"  ℹ {version_line}")
    except Exception as e:
        print(f"  ⚠ Could not get ffmpeg version: {e}")
    
    return True

def check_system_capabilities():
    """Check hardware encoding capabilities."""
    print("\nChecking system capabilities...")
    
    try:
        # Import our system detection module
        sys.path.insert(0, str(Path(__file__).parent / 'src'))
        from system_utils import SystemCapabilities
        
        sys_caps = SystemCapabilities()
        
        print(f"  ℹ System: {sys_caps.os_type}")
        print(f"  ℹ CPU cores: {sys_caps.cpu_count}")
        print(f"  ℹ RAM: {sys_caps.memory_gb:.1f} GB")
        
        # Check encoders
        encoders = []
        if sys_caps.has_nvenc:
            encoders.append("NVIDIA NVENC")
        if sys_caps.has_qsv:
            encoders.append("Intel QSV")
        if sys_caps.has_vaapi:
            encoders.append("AMD VAAPI")
        if sys_caps.has_videotoolbox:
            encoders.append("Apple VideoToolbox")
        
        if encoders:
            print(f"  ✓ Hardware encoders: {', '.join(encoders)}")
        else:
            print("  ℹ No hardware encoders detected (will use CPU)")
        
        encoder_name, _ = sys_caps.get_optimal_encoder()
        workers = sys_caps.get_optimal_workers()
        print(f"  ℹ Selected encoder: {encoder_name}")
        print(f"  ℹ Parallel workers: {workers}")
        
        return True
        
    except Exception as e:
        print(f"  ⚠ Could not check system capabilities: {e}")
        return True  # Non-fatal

def check_directory_structure():
    """Check directory structure."""
    print("\nChecking directory structure...")
    
    base_dir = Path(__file__).parent
    input_dir = base_dir / "input"
    src_dir = base_dir / "src"
    
    if not src_dir.exists():
        print(f"  ✗ src/ directory not found")
        return False
    print(f"  ✓ src/ directory exists")
    
    if not input_dir.exists():
        print(f"  ⚠ input/ directory not found (will be created)")
        print(f"    Place your Snapchat export in: {input_dir}/")
    else:
        print(f"  ✓ input/ directory exists")
        
        # Check for export
        found_export = False
        for item in input_dir.iterdir():
            if item.is_dir():
                if (item / "json").exists() and (item / "chat_media").exists():
                    print(f"  ✓ Found Snapchat export: {item.name}")
                    found_export = True
                    break
        
        if not found_export:
            print(f"  ℹ No Snapchat export found in input/")
            print(f"    Place your export folder (e.g., 'mydata') in: {input_dir}/")
    
    return True

def main():
    """Run all checks."""
    print("=" * 60)
    print("  Snapchat Media Mapper - Setup Verification")
    print("=" * 60)
    
    checks = [
        check_python_version(),
        check_dependencies(),
        check_ffmpeg(),
        check_system_capabilities(),
        check_directory_structure()
    ]
    
    print("\n" + "=" * 60)
    if all(checks):
        print("✓ All checks passed! You're ready to run the tool.")
        print("\nTo start processing:")
        print("  python src/main.py")
    else:
        print("✗ Some checks failed. Please fix the issues above.")
        return 1
    print("=" * 60)
    
    return 0

if __name__ == "__main__":
    sys.exit(main())

