# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Snapchat media mapper that processes unzipped Snapchat data exports to organize chat history and media files. It intelligently merges video overlays, maps media to messages, and structures conversations for browsing.

## Common Development Commands

### Running the Application
```bash
# Main execution
python src/main.py

# With arguments
python src/main.py --input input --output output --log-level INFO
python src/main.py --no-clean  # Preserves existing output directory
```

### Testing Single Components
```bash
# Test media processing
python -c "from src.media_processing import extract_mp4_timestamp; print(extract_mp4_timestamp(Path('test.mp4')))"

# Test conversation processing
python -c "from src.conversation import process_friends_data; import json; print(process_friends_data(json.loads('{\"Friends\":[]}')))"
```

### Dependencies
- Python 3.7+ with standard libraries only (no pip packages required)
- FFmpeg must be installed and available on system PATH

## Architecture & Processing Pipeline

### Core Processing Flow
The application follows a strict 7-phase pipeline with statistics tracking:

1. **INITIALIZATION** → Find export folder in `input/`, validate structure
2. **OVERLAY MERGING** → Use FFmpeg with multiprocessing to merge videos with overlay filters
3. **COPY UNMERGED FILES** → Transfer non-merged media to temp directory
4. **DATA LOADING** → Process JSON files (chat_history, snap_history, friends)
5. **MEDIA MAPPING** → Two-phase mapping: first by Media ID, then by timestamp matching
6. **OUTPUT ORGANIZATION** → Create conversation-based folder structure
7. **CLEANUP** → Handle orphaned media and remove temporary files

### Critical Architecture Decisions

**Parallel Processing with Multiprocessing**
- Uses `multiprocessing.Pool` for FFmpeg operations (not threading)
- Worker function `_ffmpeg_worker` must be top-level and picklable
- CPU cores: `max(1, cpu_count() - 1)` for optimal performance

**Media Mapping Strategy**
- Primary: Extract Media ID from filename patterns using regex
- Fallback: Extract QuickTime timestamps from MP4 atoms, match within 60-second threshold
- Grouped media: Handle multipart videos and grouped snaps with folder structures

**Timestamp Extraction from MP4**
- Reads MP4 atom structure directly (no external libraries)
- Searches for 'moov' atom, then 'mvhd' for creation timestamp
- Adjusts for QuickTime epoch (2082844800 seconds offset from Unix epoch)

### File Organization Patterns

**Input Structure Expected:**
```
input/
└── mydata_[timestamp]/
    ├── json/
    │   ├── chat_history.json
    │   ├── snap_history.json
    │   └── friends.json
    └── chat_media/
        └── [date]-[id]_media~[hash].mp4
```

**Output Structure Generated:**
```
output/
├── conversations/
│   └── [date] - [username]/
│       ├── media/
│       └── conversation.json
├── groups/
│   └── [date] - [group_name]/
└── orphaned/
```

### Key Data Flows

**Media ID Extraction Patterns:**
- `b~[id]` format for certain media types
- `media~zip-[UUID]` for compressed media
- `media~[UUID]` or `overlay~[UUID]` for standard files

**Conversation Metadata Structure:**
Each conversation.json contains:
- `conversation_metadata`: Type, participants, date range, message counts
- `messages`: Array with original data plus `media_locations`, `matched_media_files`, `mapping_method`

## Important Implementation Notes

### FFmpeg Integration
- Filter complex for overlay merging: `[1:v][0:v]scale=w=rw:h=rh,format=rgba[ovr];[0:v][ovr]overlay=0:0`
- Preserves original file timestamps after merge
- 300-second timeout per FFmpeg operation

### Error Handling Patterns
- Graceful fallback when FFmpeg unavailable (skips overlay merging)
- Continues processing on individual file failures
- Comprehensive statistics tracking for each phase
- Detailed logging with configurable levels

### Performance Considerations
- Multiprocessing pool for parallel FFmpeg operations
- Batch processing of files grouped by date
- Memory-efficient streaming for MP4 atom parsing
- Hash-based duplicate detection for overlay files

## Module Responsibilities

- **main.py**: Orchestration, phase management, statistics aggregation
- **config.py**: Settings, paths, utility functions (JSON, timestamps, sanitization)
- **conversation.py**: JSON processing, metadata generation, participant extraction
- **media_processing.py**: FFmpeg operations, MP4 parsing, media-to-message mapping