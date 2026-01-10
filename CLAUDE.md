# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Snapchat Media Mapper - A Python tool that processes Snapchat export data and organizes media by date, conversation, and media type. All timestamps are converted to Faroese Atlantic Time (Atlantic/Faroe timezone) for day-based organization.

## Essential Commands

### Running the Tool

```bash
# Activate virtual environment first
source venv/bin/activate

# Basic usage (processes input/ folder, outputs to output/)
python src/main.py

# Custom directories
python src/main.py --input /path/to/export --output /path/to/output

# Preserve existing output (don't clean)
python src/main.py --no-clean

# Debug logging
python src/main.py --log-level DEBUG
```

### Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Note: ffmpeg must be installed separately (see README.md)
```

## Architecture Overview

### Processing Pipeline

The tool executes a multi-phase pipeline in `src/main.py`:

1. **Initialization** - Find export folder, clean output directory
2. **Overlay Merging** - Merge `_media~` and `_overlay~` video pairs using ffmpeg
3. **Data Loading** - Load JSON files (chat_history, snap_history, friends)
4. **Media Indexing** - Build searchable index of all media files by Media ID
5. **Media Mapping** - Map media to messages using two-phase strategy (see below)
6. **Day-Based Organization** - Group by Faroese calendar day, create output structure
7. **Bitmoji Generation** - Fetch/generate avatar images for all users
8. **Cleanup** - Remove temporary directories and cache

### Critical Data Flow

```
Snapchat Export (input/)
├── chat_media/          # All media files (_media~, _overlay~)
└── json/                # Metadata (chat_history.json, snap_history.json, friends.json)
    ↓
Overlay Merging (temp directory, auto-cleaned)
    ↓
Media Index (by Media ID + timestamp extraction)
    ↓
Media Mapping (two-phase: ID-based → timestamp fallback)
    ↓
Day Grouping (Faroese timezone conversion)
    ↓
Output Structure (output/)
├── index.json           # Master index with users/groups
├── bitmoji/            # Avatar SVGs
└── days/               # Organized by YYYY-MM-DD
    └── YYYY-MM-DD/
        ├── conversations.json
        ├── media/      # Mapped media files
        └── orphaned/   # Unmapped media files
```

## Module Responsibilities

### `src/main.py` - Orchestration

- **Pipeline execution**: Coordinates all processing phases
- **Signal handling**: Ctrl+C cleanup with graceful shutdown
- **Temporary directory management**: Creates temp dirs for merging, registers for auto-cleanup
- **Orphan rescue**: Attempts to map orphaned media using type-based heuristics
- **Statistics tracking**: Comprehensive logging of processing results

**Key Functions:**

- `find_export_folder()` - Validates Snapchat export structure (requires `json/` and `chat_media/`)
- `_rescue_orphaned_media()` - Smart matching for unmapped media (uses media type + message type compatibility)
- `_log_final_summary()` - Detailed processing statistics

### `src/media_processing.py` - Media Pipeline

- **Overlay merging**: Parallel ffmpeg encoding (uses libx264 CPU encoding)
- **Media indexing**: Extract Media IDs from filenames, lazy timestamp extraction
- **Media mapping**: Two-phase strategy (see Media Mapping Strategy below)
- **Binary search optimization**: O(log n) timestamp lookups using sorted indices

**Key Functions:**

- `merge_overlay_pairs()` - Detects video/overlay pairs by date and filename patterns
- `index_media_files()` - Builds `{media_id: MediaFile}` index, probes for audio streams
- `map_media_to_messages()` - Phase 1: ID-based mapping → Phase 2: Timestamp-based mapping
- `extract_mp4_timestamp_fast()` - Uses ffprobe to extract creation timestamps
- `has_audio_stream()` - Used for orphan rescue (videos without audio = NOTE type)

### `src/conversation.py` - Metadata & Organization

- **Conversation merging**: Combines chat_history and snap_history by conversation ID
- **Day grouping**: Groups messages by Faroese calendar day (handles DST)
- **Metadata generation**: Creates conversation metadata (type, participants, message counts)
- **Timezone conversion**: ALL timestamps converted from UTC to Atlantic/Faroe

**Key Functions:**

- `merge_conversations()` - Merges chat/snap data, sorts by timestamp
- `group_messages_by_day()` - Returns `{date: {conv_id: [messages]}}` grouped by Faroese day
- `utc_to_faroese()` (in config.py) - Converts UTC milliseconds to Faroese datetime
- `create_conversation_metadata()` - Generates metadata with participant info from friends.json
- `_is_group_message()` - Detects group chats (has "Conversation Title" != "NULL")

### `src/bitmoji.py` - Avatar Generation

- **Bitmoji fetching**: Parallel API requests (up to 128 concurrent workers)
- **Fallback generation**: Creates unique color-coded ghost SVGs when API fails
- **Color separation**: Deterministic but visually distinct colors per username

**Key Functions:**

- `generate_bitmoji_assets()` - Main entry point: fetch → save → return paths
- `get_all_avatars()` - Parallel fetching using ThreadPoolExecutor
- `FallbackGenerator._get_distinct_color()` - Ensures minimum 15° hue separation

### `src/config.py` - Shared Utilities

- **Data models**: `MediaFile`, `Stats` dataclasses
- **Timezone utilities**: Faroese timezone conversion and formatting
- **File operations**: `safe_materialize()` (tries hardlink → fallback to copy)
- **Constants**: Timestamp threshold (60s), media type mappings

**Important Constants:**

- `FAROESE_TZ = pytz.timezone('Atlantic/Faroe')` - All dates use this timezone
- `TIMESTAMP_THRESHOLD_SECONDS = 60` - Timestamp mapping tolerance
- `QUICKTIME_EPOCH_ADJUSTER = 2082844800` - QuickTime epoch offset (not currently used)

## Media Mapping Strategy

Critical two-phase algorithm in `media_processing.py`:

### Phase 1: Media ID Mapping (Exact)

- Matches media files using `Media IDs` field from messages
- 100% accuracy when IDs are available
- **Special handling for snaps**: Only maps first Media ID (prevents duplicates)

### Phase 2: Timestamp Mapping (Fuzzy)

For unmapped media files:

1. **Lazy timestamp extraction**: Only extract timestamps for unmapped MP4 files
2. **Binary search optimization**: Build sorted timestamp index for O(log n) lookups
3. **60-second threshold**: Finds messages within 60s of media creation time
4. **Empty snap prioritization**: Prefers snaps without existing media
5. **Fallback to locked snaps**: If no empty snaps found, adds to existing snap to prevent orphaning

**Implementation Details:**

- `build_timestamp_index()` creates sorted timestamp list for binary search
- `find_timestamp_matches()` uses `bisect_left/right` for range queries
- Parallel timestamp extraction (8 workers) using ThreadPoolExecutor

## Orphan Rescue System

After media mapping, `main.py` attempts to rescue unmapped media using type-based heuristics:

**Compatibility Matrix:**

- Image files (jpg, png, etc.) → Can match IMAGE or MEDIA message types
- Video with audio → Can match VIDEO or MEDIA message types
- Video without audio → Can ONLY match NOTE message types

**Rescue Process:**

1. Find messages without media for this day
2. Check type compatibility using `has_audio_stream()` for videos
3. Only rescue if exactly 1 compatible message exists (unambiguous)
4. Update message with `_media` and `mapping_method: 'orphan_rescue'`

## Timezone Handling

**Critical**: All date-based organization uses Faroese Atlantic Time (UTC-1 winter / UTC+0 summer with DST)

**Why Faroese Time?**

- Original data timestamps are in UTC
- Day boundaries must be consistent with a single timezone
- Faroese time chosen as the reference timezone for this project

**Conversion Flow:**

1. Messages have `Created(microseconds)` field (actually milliseconds, mislabeled)
2. `utc_to_faroese()` converts to Atlantic/Faroe timezone
3. `get_faroese_date()` extracts YYYY-MM-DD date string
4. `format_faroese_timestamp()` formats as "YYYY-MM-DD HH:MM:SS.mmm Atlantic/Faroe"

**Important**: When modifying date/time logic, all operations must account for DST transitions.

## Parallel Processing

The tool uses parallel processing in several phases:

1. **Overlay Merging** - ThreadPoolExecutor with 4 workers (default)
   - Location: `media_processing.py:merge_overlay_pairs()`
   - Encoding is CPU-bound, so limited workers to prevent CPU thrashing

2. **Timestamp Extraction** - ThreadPoolExecutor with 8 workers
   - Location: `media_processing.py:map_media_to_messages()` Phase 2
   - I/O-bound ffprobe operations, more workers beneficial

3. **Bitmoji Fetching** - ThreadPoolExecutor with up to 128 workers
   - Location: `bitmoji.py:get_all_avatars()`
   - Network-bound API requests, high concurrency safe

**Performance Tuning:**

- Overlay merging uses `libx264` with `ultrafast` preset and CRF 23
- Audio preservation: Checks for audio streams and includes in merge
- Progress bars (tqdm) for all parallel operations

## Temporary File Management

**Critical for cleanup**:

- `temp_merged_dir` created in export parent directory: `export.parent / f"temp_merged_{timestamp}"`
- `.cache/` directory for WebP conversions (currently unused, ffmpeg reads WebP directly)
- Both registered with `register_temp_directory()` for auto-cleanup

**Cleanup Triggers:**

1. Normal completion (cleanup phase)
2. Exception/error (try-catch in main)
3. Signal interrupt (Ctrl+C via `signal_handler()`)
4. Process exit (`atexit.register()`)

## Output Structure Details

### `output/index.json`

```json
{
  "account_owner": "username",
  "users": [{"username": "...", "display_name": "...", "bitmoji": "bitmoji/file.svg"}],
  "groups": [{"group_id": "uuid", "name": "...", "members": ["user1", "user2"]}]
}
```

### `output/days/YYYY-MM-DD/conversations.json`

```json
{
  "date": "YYYY-MM-DD",
  "stats": {"conversationCount": N, "messageCount": N, "mediaCount": N},
  "conversations": [
    {
      "id": "folder_name",
      "conversation_id": "original_id",
      "conversation_type": "individual|group",
      "messages": [
        {
          "Type": "message|snap",
          "From": "username",
          "Created": "YYYY-MM-DD HH:MM:SS.mmm Atlantic/Faroe",
          "media_locations": ["media/filename.mp4"],
          "matched_media_files": ["filename.mp4"],
          "mapping_method": "media_id|timestamp|orphan_rescue"
        }
      ]
    }
  ],
  "orphanedMedia": {
    "orphaned_media_count": N,
    "orphaned_media": [{"path": "orphaned/file.mp4", "filename": "...", "type": "VIDEO"}]
  }
}
```

## Common Development Patterns

### Adding New Processing Phases

1. Create phase in `main()` between existing phases
2. Add phase timer: `phase_start = time.time()` → `stats.phase_times['phase_name'] = time.time() - phase_start`
3. Add phase header: `_log_phase_header("PHASE NAME")`
4. Update `_log_final_summary()` to include phase in timing report

### Modifying Media Mapping

- Phase 1 (ID-based): Modify `map_media_to_messages()` Phase 1 section
- Phase 2 (timestamp): Modify threshold in `config.py:TIMESTAMP_THRESHOLD_SECONDS`
- Add new mapping method: Update `mapping_method` field and stats tracking

### Changing Timezone

- Update `config.py:FAROESE_TZ` to desired timezone
- All date grouping and formatting will automatically use new timezone
- **Warning**: Changing timezone mid-project will create inconsistent day boundaries

## Dependencies

**Critical System Requirements:**

- `ffmpeg` and `ffprobe` must be in PATH (video processing and timestamp extraction)
- Python 3.8+ required (uses dataclasses, type hints)

**Key Python Libraries:**

- `ffmpeg-python` - Python bindings for ffmpeg operations
- `pytz` - Timezone conversions (Faroese timezone)
- `tqdm` - Progress bars for all phases
- `requests` - Bitmoji API calls with retry logic
- `Pillow` - Image processing (WebP handling)

## Debugging Tips

**Enable debug logging:**

```bash
python src/main.py --log-level DEBUG
```

**Check media mapping issues:**

- Look for "Mapped by Media ID" vs "Mapped by timestamp" counts in logs
- High orphan count → Check timestamp extraction or Media ID format changes
- Check `orphaned_media_count` in `conversations.json` files

**Verify overlay merging:**

- Check temp directory exists during processing: `export.parent/temp_merged_*`
- Look for "Completed X/Y merge operations" in logs
- If merges fail, check ffmpeg installation: `ffmpeg -version`

**Timezone issues:**

- Verify dates in output match expected Faroese time (UTC-1 winter / UTC+0 summer)
- Check `conversations.json` "Created" fields show "Atlantic/Faroe" suffix
- Compare with original UTC timestamps in `Created(microseconds)` field

## Performance Considerations

**Bottlenecks:**

1. Overlay merging (CPU-bound) - Fastest with hardware encoders (currently uses libx264 CPU)
2. Timestamp extraction (I/O-bound) - Parallelized with 8 workers
3. Bitmoji fetching (network-bound) - Parallelized with up to 128 workers

**Optimization Opportunities:**

- Hardware acceleration for overlay merging (NVENC, QSV, VideoToolbox) - infrastructure exists but currently uses libx264
- Increase timestamp extraction workers if I/O permits
- Cache media index across runs for incremental processing
