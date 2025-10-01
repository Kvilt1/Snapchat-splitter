# Snapchat Media Mapper

A powerful Python tool to organize and map your Snapchat export data by date, conversation, and media type. It automatically merges overlay media (text/drawings on videos), maps media to messages, and organizes everything by calendar day in the Faroese timezone.

## Features

- 🎯 **Smart Media Mapping**: Maps media files to messages using Media IDs and timestamps
- 🎬 **Overlay Merging**: Automatically merges video overlays (text, drawings) with base videos
- 📅 **Day-Based Organization**: Organizes all content by calendar day (Faroese timezone)
- 🎨 **Bitmoji Integration**: Automatically fetches and generates avatar images for all users
- ⚡ **Hardware Acceleration**: Auto-detects and uses GPU encoders (NVIDIA NVENC, Intel QSV, AMD VAAPI, macOS VideoToolbox)
- 🔄 **Cross-Platform**: Works on Windows, macOS, and Linux
- 🚀 **Parallel Processing**: Multi-threaded encoding and file processing for speed
- 📊 **Progress Tracking**: Real-time progress bars and detailed statistics
- 🧹 **Smart Cleanup**: Automatic cleanup of temporary files on completion or interruption

## Table of Contents

- [Requirements](#requirements)
- [Installation](#installation)
  - [Windows](#windows)
  - [macOS](#macos)
  - [Linux](#linux)
- [Setup](#setup)
- [Usage](#usage)
- [Input Structure](#input-structure)
- [Output Structure](#output-structure)
- [How It Works](#how-it-works)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

## Requirements

### System Requirements

- **Python**: 3.8 or higher
- **ffmpeg**: Required for video processing (see installation instructions below)
- **RAM**: 4GB minimum, 8GB+ recommended for large exports
- **Storage**: At least 2x the size of your Snapchat export (for processing)

### Optional Hardware Acceleration

- **NVIDIA GPU**: For NVENC hardware encoding (2-5x faster)
- **Intel CPU with Quick Sync**: For QSV hardware encoding
- **AMD GPU**: For VAAPI hardware encoding (Linux)
- **macOS**: Uses VideoToolbox hardware encoding

## Installation

### 1. Install Python

Make sure you have Python 3.8 or higher installed:

```bash
python --version
# or
python3 --version
```

If not installed, download from [python.org](https://www.python.org/downloads/)

### 2. Install ffmpeg

ffmpeg is **required** for video processing. Choose your platform:

#### Windows

**Option A: Using Chocolatey (Recommended)**
```powershell
# Install Chocolatey if not already installed (run as Administrator)
# See https://chocolatey.org/install

# Install ffmpeg
choco install ffmpeg
```

**Option B: Manual Installation**
1. Download ffmpeg from [ffmpeg.org/download.html](https://ffmpeg.org/download.html)
2. Extract the archive
3. Add the `bin` folder to your system PATH
4. Verify: `ffmpeg -version`

#### macOS

**Using Homebrew (Recommended)**
```bash
# Install Homebrew if not already installed
# See https://brew.sh

# Install ffmpeg
brew install ffmpeg
```

**Using MacPorts**
```bash
sudo port install ffmpeg
```

#### Linux

**Debian/Ubuntu:**
```bash
sudo apt update
sudo apt install ffmpeg
```

**Fedora:**
```bash
sudo dnf install ffmpeg
```

**Arch Linux:**
```bash
sudo pacman -S ffmpeg
```

**Verify Installation:**
```bash
ffmpeg -version
ffprobe -version
```

### 3. Clone or Download This Repository

```bash
git clone <repository-url>
cd Snapchat-splitter
```

Or download and extract the ZIP file.

### 4. Install Python Dependencies

**Option A: Using Virtual Environment (Recommended)**

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

**Option B: System-Wide Installation**

```bash
pip install -r requirements.txt
```

## Setup

### 1. Get Your Snapchat Export

1. Open Snapchat and go to **Settings** → **My Data**
2. Request your data export (can take 24-72 hours)
3. Download the ZIP file when ready
4. Extract it (you'll get a folder like `mydata`)

### 2. Place Your Export in the Input Folder

```bash
# Your directory structure should look like:
Snapchat-splitter/
├── input/
│   └── mydata/              # Your extracted Snapchat export
│       ├── chat_media/      # All your media files
│       └── json/            # All the JSON metadata files
├── output/                  # Will be created automatically
├── src/                     # Source code
└── requirements.txt
```

### 3. Verify Your Setup

```bash
# Check that ffmpeg is installed
ffmpeg -version

# Check that Python dependencies are installed
python -c "import ffmpeg; import PIL; print('Dependencies OK')"
```

## Usage

### Basic Usage

```bash
# Activate virtual environment if you created one
source venv/bin/activate  # macOS/Linux
# or
venv\Scripts\activate      # Windows

# Run the tool
python src/main.py
```

### Advanced Options

```bash
# Custom input/output directories
python src/main.py --input /path/to/export --output /path/to/output

# Keep existing output (don't clean)
python src/main.py --no-clean

# Change log level
python src/main.py --log-level DEBUG

# Show help
python src/main.py --help
```

### What to Expect

When you run the tool, it will:

1. **Detect your system** - Identifies available hardware encoders
2. **Find your export** - Locates the Snapchat export in `input/`
3. **Merge overlays** - Combines videos with text/drawing overlays
4. **Load data** - Reads all JSON files
5. **Index media** - Creates a searchable index of all media files
6. **Map media** - Links media files to messages
7. **Organize by day** - Splits everything into day folders
8. **Clean up** - Removes temporary files

Processing time varies based on:
- Size of your export (number of files)
- Hardware capabilities (GPU vs CPU)
- System resources

Typical speeds:
- **With GPU**: 5-15 videos/second
- **With CPU**: 1-3 videos/second

## Input Structure

Your Snapchat export should have this structure:

```
input/
└── mydata/                          # Your export folder
    ├── chat_media/                  # All media files
    │   ├── 2024-01-15_media~ABC123.mp4
    │   ├── 2024-01-15_overlay~ABC123.webp
    │   ├── 2024-01-15_media~DEF456.jpg
    │   └── ...
    └── json/
        ├── chat_history.json        # Chat messages
        ├── snap_history.json        # Snaps
        ├── friends.json             # Friend list
        ├── account.json             # Account info
        └── ...                      # Other metadata
```

## Output Structure

The tool creates this organized structure:

```
output/
├── index.json                       # Master index with all users/groups
├── bitmoji/                         # Avatar images for all users
│   ├── username1.svg                # Bitmoji or fallback avatar
│   ├── username2.svg
│   └── ...
└── days/                            # Organized by date
    ├── 2024-01-15/                  # Each day has its own folder
    │   ├── conversations.json       # All conversations for this day
    │   ├── media/                   # All media files for this day
    │   │   ├── 2024-01-15_media~ABC123.mp4  # Videos, images, etc.
    │   │   ├── 2024-01-15_media~DEF456.jpg
    │   │   └── ...
    │   └── orphaned/                # Media that couldn't be mapped
    │       └── 2024-01-15_media~XYZ789.mp4
    ├── 2024-01-16/
    │   └── ...
    └── ...
```

### Key Files Explained

#### `output/index.json`

Master index containing:
- Account owner information
- All users with display names
- All groups with members
- Summary of your export

Example:
```json
{
  "account_owner": "your_username",
  "users": [
    {
      "username": "friend1",
      "display_name": "Friend One",
      "bitmoji": "bitmoji/friend1.svg"
    }
  ],
  "groups": [
    {
      "group_id": "uuid-here",
      "name": "Group Chat Name",
      "members": ["friend1", "friend2"]
    }
  ]
}
```

#### `output/days/YYYY-MM-DD/conversations.json`

Contains all conversations for that specific day:

```json
{
  "date": "2024-01-15",
  "stats": {
    "conversationCount": 5,
    "messageCount": 127,
    "mediaCount": 43
  },
  "conversations": [
    {
      "id": "friend_username",
      "conversation_id": "friend_username",
      "conversation_type": "individual",
      "messages": [
        {
          "Type": "message",
          "From": "friend_username",
          "Created": "2024-01-15 14:30:45.123 Atlantic/Faroe",
          "Body": "Hey! Check this out",
          "media_locations": ["media/2024-01-15_media~ABC123.jpg"],
          "matched_media_files": ["2024-01-15_media~ABC123.jpg"],
          "mapping_method": "media_id"
        }
      ]
    }
  ],
  "orphanedMedia": {
    "orphaned_media_count": 2,
    "orphaned_media": [
      {
        "path": "orphaned/2024-01-15_media~XYZ789.mp4",
        "filename": "2024-01-15_media~XYZ789.mp4",
        "type": "VIDEO",
        "extension": "mp4"
      }
    ]
  }
}
```

## How It Works

### 1. Overlay Merging

Snapchat stores some videos in two parts:
- **Base video** (`_media~...`): The raw video
- **Overlay** (`_overlay~...`): Text, drawings, stickers

The tool automatically:
1. Detects overlay pairs by matching filenames and dates
2. Converts WebP overlays to PNG for better compatibility
3. Merges them using ffmpeg with hardware acceleration
4. Preserves audio and metadata

### 2. Media Mapping

Media is mapped to messages in two ways:

**Method 1: Media ID (Exact)**
- Matches media files using the Media IDs in messages
- 100% accurate when IDs are available
- Used for most chat messages

**Method 2: Timestamp (Fuzzy)**
- For media without IDs, matches by timestamp
- Finds messages within 60 seconds of media creation time
- Prioritizes empty snap messages to avoid duplicates

### 3. Day-Based Organization

All content is organized by **Faroese calendar day**:
- UTC timestamps are converted to Atlantic/Faroe timezone
- Messages/media are grouped by the day they occurred (in Faroese time)
- Each day gets its own folder with all conversations and media

### 4. Hardware Acceleration

The tool automatically detects and uses the best encoder:

| Platform | Encoder | Priority |
|----------|---------|----------|
| NVIDIA GPU | NVENC (h264_nvenc) | 1st |
| macOS | VideoToolbox | 2nd |
| Intel CPU | Quick Sync (h264_qsv) | 3rd |
| AMD GPU (Linux) | VAAPI (h264_vaapi) | 4th |
| Any | CPU (libx264) | Fallback |

If hardware encoding fails, it automatically falls back to CPU encoding.

### 5. Bitmoji Avatar Generation

The tool automatically fetches Bitmoji avatars for all users:
- Fetches real Bitmoji from Snapchat's API when available
- Generates unique, color-coded fallback avatars for users without Bitmoji
- Saves all avatars as SVG files in `output/bitmoji/`
- Updates index.json with relative paths to each avatar
- Uses parallel processing (up to 128 concurrent requests) for speed

Fallback avatars are:
- Deterministic (same username always gets same color)
- Visually distinct (uses color separation algorithm)
- Ghost-themed to match Snapchat's brand

### 6. Parallel Processing

The tool uses parallel processing for:
- **Overlay merging**: Multiple videos encoded simultaneously
- **WebP conversion**: Batch conversion of overlay images
- **Timestamp extraction**: Parallel MP4 metadata reading
- **Bitmoji fetching**: Concurrent API requests for avatar images

Worker count is auto-detected based on:
- Available hardware encoders
- CPU cores
- System RAM

## Configuration

You can customize behavior by editing `src/config.py`:

```python
# Encoding Configuration
GPU_WORKERS = None  # Auto-detect (recommended)
# Or set manually:
# GPU_WORKERS = 6   # For systems with hardware encoders
# GPU_WORKERS = 2   # For CPU encoding

# Timestamp matching threshold
TIMESTAMP_THRESHOLD_SECONDS = 60  # Match media within 60 seconds

# Timezone (for organizing by day)
FAROESE_TZ = pytz.timezone('Atlantic/Faroe')

# Performance tuning
USE_FAST_TIMESTAMP_EXTRACTION = True  # Use ffprobe (faster)
WEBP_CONVERSION_WORKERS = 8           # Parallel WebP conversion
```

### Optimizing Worker Count

Monitor your system and adjust if needed:

**NVIDIA GPU:**
```bash
# Watch GPU usage while processing
watch -n 1 nvidia-smi
```

- If GPU utilization < 80%: Increase `GPU_WORKERS`
- If GPU utilization = 100% and slow: Decrease `GPU_WORKERS`
- If VRAM is maxed out: Decrease `GPU_WORKERS`

**CPU Encoding:**
```bash
# Watch CPU usage
htop  # Linux/macOS
# or Task Manager on Windows
```

- Generally use half your CPU cores for CPU encoding
- More workers = higher CPU usage but faster processing

## Troubleshooting

### "ffmpeg not found in PATH"

**Solution**: Install ffmpeg (see [Installation](#installation))

Verify with:
```bash
ffmpeg -version
```

### "No valid Snapchat export found"

**Solution**: Make sure your export folder has this structure:
```
input/
└── mydata/
    ├── chat_media/
    └── json/
```

### Hardware Encoding Fails

**Symptoms**: Log shows "Hardware encoding failed, falling back to CPU"

**Solutions**:
1. **NVIDIA GPU**: Update GPU drivers
2. **Intel QSV**: Ensure latest graphics drivers installed
3. **macOS**: Update to latest macOS version
4. If all else fails, CPU encoding will work (just slower)

### Out of Memory Errors

**Solutions**:
1. Reduce `GPU_WORKERS` in `src/config.py`
2. Close other applications
3. Process in smaller batches using `--no-clean` flag

### Slow Processing Speed

**Solutions**:
1. Check if hardware encoder is being used (check logs)
2. Increase `GPU_WORKERS` if GPU utilization is low
3. Close background applications
4. Ensure input files are on a fast drive (SSD preferred)

### Permission Errors

**Windows**: Run as Administrator
**Linux/macOS**: Check file permissions
```bash
chmod -R u+rw input/ output/
```

### Import Errors

**Solution**: Reinstall dependencies
```bash
pip install --upgrade -r requirements.txt
```

## Statistics and Monitoring

The tool provides detailed statistics:

- Total media files discovered
- Successfully processed files
- Overlay merge rate
- Media mapping results (by ID vs timestamp)
- Orphaned media count
- Processing time per phase

Example output:
```
=========================================================
         PROCESSING COMPLETE - SUMMARY
=========================================================
Total media files discovered:        2,847
Successfully processed:              2,841
  - Processing rate:                 99.8%
  - Merged with overlays:            1,234

Mapping Results:
  - Mapped by Media ID:              2,156
  - Mapped by timestamp:             685
  - Total mapped:                    2,841
  - Orphaned (unmapped):             6
  - Mapping success rate:            99.8%

Processing Time:
  - Initialization                   0.5s
  - Overlay Merging                  145.3s
  - Data Loading                     2.1s
  - Media Mapping                    8.7s
  - Output Organization              15.2s
  - Cleanup                          1.0s
  - Total                            172.8s
=========================================================
```

## Advanced Features

### Interrupt Handling

Press `Ctrl+C` at any time to:
- Stop processing gracefully
- Clean up temporary files
- Close worker pools properly

### Hardlink Support

On supported filesystems (Linux ext4, NTFS, APFS), the tool uses hardlinks instead of copying files, saving disk space and time.

### Smart Caching

- WebP conversions are cached to avoid re-processing
- Temporary files are stored in `.cache/`
- All caches are cleaned up automatically

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review the logs for error messages
3. [Create an issue on GitHub/contact information]

---

**Note**: This tool processes your Snapchat export data locally on your machine. No data is uploaded or shared anywhere.

