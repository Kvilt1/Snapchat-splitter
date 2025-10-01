# Quick Start Guide

Get up and running in 5 minutes!

## Prerequisites

1. **Python 3.8+** installed
2. **ffmpeg** installed (see [README.md](README.md) for installation)

## Installation

```bash
# 1. Clone or download this repository
cd Snapchat-splitter

# 2. Create virtual environment (recommended)
python -m venv venv

# 3. Activate virtual environment
source venv/bin/activate  # macOS/Linux
# or
venv\Scripts\activate      # Windows

# 4. Install dependencies
pip install -r requirements.txt

# 5. Verify setup
python verify_setup.py
```

## Setup Your Export

```bash
# 1. Place your Snapchat export in the input folder
mkdir -p input
# Copy/move your 'mydata' folder here

# Your structure should be:
# input/
# └── mydata/
#     ├── chat_media/
#     └── json/
```

## Run

```bash
# Basic usage
python src/main.py

# That's it! Check the output/ folder when done.
```

## Command Line Options

```bash
# Custom directories
python src/main.py --input /path/to/export --output /path/to/output

# Keep existing output (don't delete)
python src/main.py --no-clean

# Verbose logging
python src/main.py --log-level DEBUG

# Show help
python src/main.py --help
```

## What to Expect

### Processing Time

Depends on your system and export size:

- **With GPU**: ~5-15 videos/second
- **With CPU**: ~1-3 videos/second

Example: 1,000 videos
- GPU: ~1-3 minutes
- CPU: ~5-15 minutes

### Output

All organized content in `output/`:
- `index.json` - Master index
- `days/YYYY-MM-DD/` - Each day's conversations and media

## Configuration

Edit `src/config.py` to customize:

```python
# Auto-detect workers (recommended)
GPU_WORKERS = None

# Or set manually:
GPU_WORKERS = 6  # For GPU encoding
GPU_WORKERS = 2  # For CPU encoding
```

## Troubleshooting

### "ffmpeg not found"
→ Install ffmpeg (see README.md)

### "No valid Snapchat export found"
→ Check folder structure in `input/`

### Slow processing
→ Check if GPU encoder is detected in logs
→ Adjust `GPU_WORKERS` in config.py

### Out of memory
→ Reduce `GPU_WORKERS` in config.py
→ Close other applications

## Need Help?

See [README.md](README.md) for:
- Detailed installation instructions
- Complete documentation
- Troubleshooting guide
- Output structure explanation

## Quick Reference

| Command | Purpose |
|---------|---------|
| `python verify_setup.py` | Check if everything is installed |
| `python src/main.py` | Run the tool |
| `python src/main.py --help` | Show all options |
| Ctrl+C | Stop processing and clean up |

---

**Happy organizing! 🎯**

