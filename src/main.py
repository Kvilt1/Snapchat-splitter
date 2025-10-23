#!/usr/bin/env python3
"""Main orchestration for Snapchat media mapper."""

import argparse
import logging
import shutil
import sys
import time
import signal
import atexit
from pathlib import Path
from typing import Dict, Set, List, Any
from collections import defaultdict

from config import (
    INPUT_DIR,
    OUTPUT_DIR,
    ensure_directory,
    load_json,
    save_json,
    sanitize_filename,
    safe_materialize,
    get_media_type,
    MediaFile,
    Stats,
    logger
)
from tqdm import tqdm

from media_processing import (
    merge_overlay_pairs,
    index_media_files,
    map_media_to_messages,
    has_audio_stream
)

from conversation import (
    merge_conversations,
    determine_account_owner,
    create_conversation_metadata,
    get_conversation_folder_name,
    group_messages_by_day,
    generate_index_json,
    extract_date_from_filename,
    convert_message_timestamp
)

from bitmoji import generate_bitmoji_assets

# Global cleanup registry
_temp_directories = []
_cleanup_registered = False

def register_temp_directory(temp_dir: Path):
    """Register a temporary directory for cleanup on exit."""
    global _temp_directories, _cleanup_registered
    _temp_directories.append(temp_dir)
    
    if not _cleanup_registered:
        atexit.register(cleanup_temp_directories)
        _cleanup_registered = True

def cleanup_temp_directories():
    """Clean up all registered temporary directories."""
    global _temp_directories
    
    for temp_dir in _temp_directories:
        if temp_dir.exists():
            try:
                logger.info(f"Cleaning up temporary directory: {temp_dir}")
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.warning(f"Failed to clean up {temp_dir}: {e}")
    
    # Clean up cache directory
    cache_dir = Path(".cache")
    if cache_dir.exists():
        try:
            logger.info(f"Cleaning up cache directory: {cache_dir}")
            shutil.rmtree(cache_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up cache: {e}")
    
    _temp_directories.clear()

def signal_handler(signum, frame):
    """Handle Ctrl+C and other signals."""
    logger.info("\n" + "=" * 60)
    logger.info("INTERRUPTED - Cleaning up and exiting...")
    logger.info("=" * 60)
    
    cleanup_temp_directories()
    
    logger.info("Cleanup complete. Exiting.")
    sys.exit(130)  # Standard exit code for SIGINT

def find_export_folder(input_dir: Path) -> Path:
    """Find Snapchat export folder."""
    for d in input_dir.iterdir():
        if d.is_dir() and (d / "json").exists() and (d / "chat_media").exists():
            return d
    
    raise FileNotFoundError(
        f"No valid Snapchat export found in '{input_dir}'. "
        "Place your export folder (e.g., 'mydata') inside 'input' directory."
    )

def _log_phase_header(phase_name: str) -> None:
    """Log a phase header with separators.
    
    Args:
        phase_name: Name of the phase to log
    """
    logger.info("=" * 60)
    logger.info(f"PHASE: {phase_name}")
    logger.info("=" * 60)


def _load_json_files(json_dir: Path) -> tuple[Dict, Dict, Dict]:
    """Load required JSON files with progress tracking.
    
    Args:
        json_dir: Directory containing JSON files
        
    Returns:
        Tuple of (chat_data, snap_data, friends_json)
    """
    json_files = ["chat_history.json", "snap_history.json", "friends.json"]
    with tqdm(total=len(json_files), desc="Loading JSON data", unit="files") as pbar:
        pbar.set_postfix_str("chat_history.json")
        chat_data = load_json(json_dir / "chat_history.json")
        pbar.update(1)
        
        pbar.set_postfix_str("snap_history.json")
        snap_data = load_json(json_dir / "snap_history.json")
        pbar.update(1)
        
        pbar.set_postfix_str("friends.json")
        friends_json = load_json(json_dir / "friends.json")
        pbar.update(1)
    
    return chat_data, snap_data, friends_json


def _rescue_orphaned_media(
    orphaned_media: List[MediaFile],
    day_conversations: Dict[str, List[Dict]],
    day_media_dir: Path
) -> tuple[Set[str], Dict[str, Any]]:
    """
    Rescue orphaned media when there's exactly one unambiguous match.
    
    Args:
        orphaned_media: List of orphaned MediaFile objects for this day
        day_conversations: Conversations for this day {conv_id: [messages]}
        day_media_dir: Directory for the day's media files
        
    Returns:
        (rescued_filenames, rescue_metadata) - rescued files and their mapping details
    """
    if not orphaned_media:
        return set(), {}
    
    # Build filename -> MediaFile lookup
    media_lookup = {mf.filename: mf for mf in orphaned_media}
    
    # Classify orphaned media by compatible message types
    orphan_by_type = {}  # {filename: [compatible_types]}
    
    for media_file in orphaned_media:
        ext = media_file.source_path.suffix.lower()
        compatible_types = []
        
        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
            # Image files can match IMAGE or MEDIA
            compatible_types = ['IMAGE', 'MEDIA']
        elif ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm']:
            # Video files: check for audio
            has_audio = has_audio_stream(media_file.source_path)
            if has_audio:
                # Video with audio can match VIDEO or MEDIA
                compatible_types = ['VIDEO', 'MEDIA']
            else:
                # Video without audio can only match NOTE
                compatible_types = ['NOTE']
        
        if compatible_types:
            orphan_by_type[media_file.filename] = compatible_types
    
    # Find messages without media in this day
    messages_by_type = {}  # {media_type: [(conv_id, msg_idx, message)]}
    
    for conv_id, messages in day_conversations.items():
        for msg_idx, msg in enumerate(messages):
            # Skip messages that already have media
            if msg.get('_media'):
                continue
            
            media_type = msg.get('Media Type', '')
            if media_type in ['IMAGE', 'VIDEO', 'MEDIA', 'NOTE']:
                if media_type not in messages_by_type:
                    messages_by_type[media_type] = []
                messages_by_type[media_type].append((conv_id, msg_idx, msg))
    
    # Find unambiguous matches
    rescued = {}  # {filename: (conv_id, msg_idx, media_type)}
    
    for filename, compatible_types in orphan_by_type.items():
        # Count how many messages of each compatible type exist
        eligible_messages = []
        for media_type in compatible_types:
            if media_type in messages_by_type:
                eligible_messages.extend([(conv_id, msg_idx, msg, media_type) 
                                         for conv_id, msg_idx, msg in messages_by_type[media_type]])
        
        # Only rescue if exactly 1 eligible message exists
        if len(eligible_messages) == 1:
            conv_id, msg_idx, msg, media_type = eligible_messages[0]
            rescued[filename] = (conv_id, msg_idx, media_type)
    
    # Apply rescues - materialize files and update messages
    rescued_filenames = set()
    rescue_metadata = {}
    
    for filename, (conv_id, msg_idx, media_type) in rescued.items():
        media_file = media_lookup[filename]
        # Materialize the file to the media directory
        dest = day_media_dir / media_file.filename
        if safe_materialize(media_file.source_path, dest):
            # Update the message
            msg = day_conversations[conv_id][msg_idx]
            msg.setdefault('_media', []).append({
                'media_file': media_file,
                'mapping_method': 'orphan_rescue'
            })
            
            rescued_filenames.add(media_file.filename)
            rescue_metadata[media_file.filename] = {
                'conv_id': conv_id,
                'msg_idx': msg_idx,
                'media_type': media_type
            }
    
    return rescued_filenames, rescue_metadata


def _log_final_summary(stats: Stats, total_time: float, output_dir: Path, index_stats: Dict) -> None:
    """Log the final processing summary with detailed statistics.
    
    Args:
        stats: Processing statistics object
        total_time: Total processing time in seconds
        output_dir: Output directory path
        index_stats: Index statistics dictionary
    """
    logger.info("=" * 60)
    logger.info("         PROCESSING COMPLETE - SUMMARY")
    logger.info("=" * 60)

    # Calculate totals
    total_media_discovered = stats.total_media + stats.total_overlay
    total_processed = stats.total_merged + (index_stats.get('total_files', 0) - stats.total_merged)
    total_mapped = stats.mapped_by_id + stats.mapped_by_timestamp

    logger.info(f"Total media files discovered:        {total_media_discovered}")
    logger.info(f"Successfully processed:              {total_processed}")
    if total_media_discovered > 0:
        process_pct = (total_processed / total_media_discovered) * 100
        logger.info(f"  - Processing rate:                 {process_pct:.1f}%")
    logger.info(f"  - Merged with overlays:            {stats.total_merged}")

    logger.info("")
    logger.info("Mapping Results:")
    logger.info(f"  - Mapped by Media ID:              {stats.mapped_by_id}")
    logger.info(f"  - Mapped by timestamp:             {stats.mapped_by_timestamp}")
    if stats.rescued_orphans > 0:
        logger.info(f"  - Rescued orphans (smart match):   {stats.rescued_orphans}")
    logger.info(f"  - Total mapped:                    {total_mapped + stats.rescued_orphans}")
    logger.info(f"  - Orphaned (unmapped):             {stats.orphaned}")

    if total_processed > 0:
        map_pct = ((total_mapped + stats.rescued_orphans) / total_processed) * 100
        logger.info(f"  - Mapping success rate:            {map_pct:.1f}%")

    logger.info("")
    logger.info("Processing Time:")
    for phase, duration in stats.phase_times.items():
        logger.info(f"  - {phase.replace('_', ' ').title():<30} {duration:.1f}s")
    logger.info(f"  - {'Total':<30} {total_time:.1f}s")
    
    logger.info("=" * 60)
    logger.info(f"✓ Check '{output_dir}' directory for results")
    logger.info("=" * 60)


def main():
    """Main processing function."""
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    start_time = time.time()

    parser = argparse.ArgumentParser(description="Process Snapchat export data")
    parser.add_argument("--input", type=Path, default=INPUT_DIR, help="Input directory")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--no-clean", action="store_true", help="Don't clean output directory")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))

    logger.info("=" * 60)
    logger.info("    SNAPCHAT MEDIA MAPPER - STARTING")
    logger.info("=" * 60)
    logger.info("Press Ctrl+C to stop and clean up at any time")

    stats = Stats()

    try:
        # INITIALIZATION PHASE
        phase_start = time.time()
        _log_phase_header("INITIALIZATION")

        # Clean output if requested
        if not args.no_clean and args.output.exists():
            logger.info(f"Cleaning output directory: {args.output}")
            shutil.rmtree(args.output)

        # Find export folder
        export_dir = find_export_folder(args.input)
        json_dir = export_dir / "json"
        source_media_dir = export_dir / "chat_media"

        logger.info(f"Processing export from: {export_dir}")
        stats.phase_times['initialization'] = time.time() - phase_start

        # OVERLAY MERGING PHASE
        phase_start = time.time()
        # Create a temporary directory for merged files (not in output)
        temp_merged_dir = export_dir.parent / f"temp_merged_{int(time.time())}"
        register_temp_directory(temp_merged_dir)  # Register for cleanup
        merged_files, merge_stats = merge_overlay_pairs(source_media_dir, temp_merged_dir)
        stats.total_media = merge_stats.get('total_media', 0)
        stats.total_overlay = merge_stats.get('total_overlay', 0)
        stats.total_merged = merge_stats.get('total_merged', 0)
        stats.phase_times['overlay_merging'] = time.time() - phase_start

        # DATA LOADING PHASE
        phase_start = time.time()
        _log_phase_header("DATA LOADING AND PROCESSING")

        # Load JSON files with progress indication
        chat_data, snap_data, friends_json = _load_json_files(json_dir)

        if not chat_data and not snap_data:
            raise ValueError("No chat or snap data found")

        # Process conversations
        logger.info("Merging conversation data...")
        conversations = merge_conversations(chat_data, snap_data)
        account_owner = determine_account_owner(conversations)

        logger.info(f"Loaded {len(conversations)} conversations")
        stats.phase_times['data_loading'] = time.time() - phase_start

        # MEDIA INDEXING AND MAPPING PHASE
        phase_start = time.time()
        # Index both source media and the merged media subdirectory
        merged_media_dir = temp_merged_dir / "merged_media" if temp_merged_dir.exists() else None
        media_index, index_stats = index_media_files(source_media_dir, merged_media_dir)

        mapped_files, mapping_stats = map_media_to_messages(conversations, media_index)
        stats.mapped_by_id = mapping_stats.get('mapped_by_id', 0)
        stats.mapped_by_timestamp = mapping_stats.get('mapped_by_timestamp', 0)
        stats.phase_times['media_mapping'] = time.time() - phase_start

        # DAY-BASED OUTPUT ORGANIZATION PHASE
        phase_start = time.time()
        _log_phase_header("DAY-BASED OUTPUT ORGANIZATION")

        ensure_directory(args.output)

        # Group messages by Faroese calendar day
        logger.info("Grouping messages by Faroese calendar day...")
        days_data = group_messages_by_day(conversations)
        all_days = sorted(days_data.keys())
        
        logger.info(f"Found activity across {len(all_days)} days ({all_days[0]} to {all_days[-1]})")

        # Generate index.json with all conversation metadata
        logger.info("Generating index.json...")
        index_json = generate_index_json(conversations, friends_json, account_owner, days_data)
        
        # BITMOJI GENERATION PHASE
        phase_start = time.time()
        _log_phase_header("BITMOJI GENERATION")
        
        # Extract all unique usernames
        all_usernames = {user["username"] for user in index_json["users"]}
        logger.info(f"Generating Bitmoji avatars for {len(all_usernames)} users...")
        
        # Generate and save Bitmoji avatars
        bitmoji_paths = generate_bitmoji_assets(all_usernames, args.output)
        
        # Update index.json with Bitmoji paths
        for user in index_json["users"]:
            username = user["username"]
            user["bitmoji"] = bitmoji_paths.get(username)
        
        # Save index.json with Bitmoji paths
        save_json(index_json, args.output / "index.json")
        logger.info(f"Generated index.json with {len(index_json['users'])} users and {len(index_json['groups'])} groups")
        logger.info(f"Saved {len(bitmoji_paths)} Bitmoji avatars to output/bitmoji/")
        stats.phase_times['bitmoji_generation'] = time.time() - phase_start
        logger.info("=" * 60)

        # Create days folder
        days_dir = args.output / "days"
        ensure_directory(days_dir)

        # Track orphaned media by day
        orphaned_by_day = defaultdict(list)
        for media_id, media_file in media_index.items():
            if media_file.filename not in mapped_files:
                # Skip thumbnails and overlays
                if "thumbnail" in media_file.filename.lower() or "_overlay~" in media_file.filename:
                    continue
                # Extract date from filename
                file_date = extract_date_from_filename(media_file.filename)
                if file_date:
                    orphaned_by_day[file_date].append(media_file)

        # Pre-compute conversation metadata (don't recreate for each day)
        logger.info("Pre-computing conversation metadata...")
        conv_metadata_cache = {}
        conv_folder_names = {}
        for conv_id, all_messages in conversations.items():
            if all_messages:
                metadata = create_conversation_metadata(conv_id, all_messages, friends_json, account_owner)
                conv_metadata_cache[conv_id] = metadata
                folder_name = get_conversation_folder_name(metadata, all_messages)
                conv_folder_names[conv_id] = sanitize_filename(folder_name)

        # Process each day and track mapping preservation
        total_mapped_items_after = 0
        messages_with_media = 0
        total_rescued = 0
        
        with tqdm(total=len(all_days), desc="Processing days", unit="days") as day_pbar:
            for day in all_days:
                day_dir = days_dir / day
                ensure_directory(day_dir)
                
                # Create shared media folder for this day
                day_media_dir = day_dir / "media"
                ensure_directory(day_media_dir)
                
                # Create orphaned folder
                orphaned_dir = day_dir / "orphaned"
                
                # Process conversations for this day
                day_conversations = days_data[day]
                conversations_output = []
                day_total_messages = 0
                day_total_media = 0
                
                # ORPHAN RESCUE: Try to rescue orphaned media BEFORE building output
                rescued_filenames = set()
                if day in orphaned_by_day:
                    rescued_filenames, rescue_metadata = _rescue_orphaned_media(
                        orphaned_by_day[day],
                        day_conversations,
                        day_media_dir
                    )
                    
                    if rescued_filenames:
                        logger.info(f"  Day {day}: Rescued {len(rescued_filenames)} orphaned media file(s)")
                        # Update stats
                        total_rescued += len(rescued_filenames)
                
                # Process conversations and materialize media
                for conv_id, day_messages in day_conversations.items():
                    # Get metadata for this conversation
                    all_messages = conversations[conv_id]
                    metadata = conv_metadata_cache.get(conv_id)
                    folder_name = conv_folder_names.get(conv_id)
                    
                    # Process each message, materializing media if attached
                    clean_messages = []
                    for msg in day_messages:
                        # Process media if attached
                        if '_media' in msg:
                            media_locations = []
                            matched_files = []
                            
                            for item in msg['_media']:
                                media_file = item['media_file']
                                dest = day_media_dir / media_file.filename
                                
                                if safe_materialize(media_file.source_path, dest):
                                    matched_files.append(media_file.filename)
                                media_locations.append(f"media/{media_file.filename}")
                            
                            msg['media_locations'] = media_locations
                            msg['matched_media_files'] = matched_files
                            msg['mapping_method'] = msg['_media'][0]['mapping_method']
                            msg['is_grouped'] = False
                            
                            if 'time_diff_seconds' in msg['_media'][0]:
                                msg['time_diff_seconds'] = msg['_media'][0]['time_diff_seconds']
                            
                            # Update stats
                            day_total_media += len(media_locations)
                            total_mapped_items_after += len(media_locations)
                            messages_with_media += 1
                        
                        # Clean up internal fields
                        clean_msg = {k: v for k, v in msg.items() 
                                    if k not in ('Created(microseconds)', '_media')}
                        clean_messages.append(clean_msg)
                    
                    # Build conversation entry with metadata
                    conversation_entry = {
                        "id": folder_name,
                        "conversation_id": conv_id,
                        "conversation_type": metadata.get("conversation_type") if metadata else "unknown",
                        "messages": clean_messages
                    }
                    
                    # Add group name if applicable
                    if metadata and metadata.get("conversation_type") == "group":
                        for msg in all_messages:
                            if msg.get("Conversation Title"):
                                conversation_entry["group_name"] = msg["Conversation Title"]
                                break
                    
                    conversations_output.append(conversation_entry)
                    day_total_messages += len(clean_messages)
                
                # Handle remaining orphaned media for this day
                orphaned_media_list = []
                if day in orphaned_by_day:
                    ensure_directory(orphaned_dir)
                    
                    for media_file in orphaned_by_day[day]:
                        # Skip rescued files
                        if media_file.filename in rescued_filenames:
                            continue
                        
                        safe_materialize(media_file.source_path, orphaned_dir / media_file.filename)
                        
                        # Determine media type from extension
                        ext = media_file.filename.split('.')[-1]
                        media_type = get_media_type(ext)
                        
                        orphaned_media_list.append({
                            "path": f"orphaned/{media_file.filename}",
                            "filename": media_file.filename,
                            "type": media_type,
                            "extension": ext.lower()
                        })
                
                # Build day JSON structure
                day_json = {
                    "date": day,
                    "stats": {
                        "conversationCount": len(conversations_output),
                        "messageCount": day_total_messages,
                        "mediaCount": day_total_media
                    },
                    "conversations": conversations_output,
                    "orphanedMedia": {
                        "orphaned_media_count": len(orphaned_media_list),
                        "orphaned_media": orphaned_media_list
                    }
                }
                
                # Save single conversations.json for this day
                save_json(day_json, day_dir / "conversations.json")
                
                day_pbar.update(1)

        total_orphaned = sum(len(files) for files in orphaned_by_day.values())
        # Subtract rescued files from orphaned count
        final_orphaned = total_orphaned - total_rescued
        
        logger.info(f"Organized {len(all_days)} days with {final_orphaned} orphaned media files")
        if total_rescued > 0:
            logger.info(f"Rescued {total_rescued} orphaned media files using smart matching")
        logger.info(f"Total messages with media in output: {messages_with_media}")
        logger.info(f"Total media items processed: {total_mapped_items_after}")
        
        stats.phase_times['output_organization'] = time.time() - phase_start
        stats.rescued_orphans = total_rescued
        stats.orphaned = final_orphaned

        # CLEANUP PHASE
        phase_start = time.time()
        _log_phase_header("CLEANUP")

        cleanup_temp_directories()

        logger.info("Cleanup complete")
        stats.phase_times['cleanup'] = time.time() - phase_start

        # FINAL SUMMARY
        total_time = time.time() - start_time
        _log_final_summary(stats, total_time, args.output, index_stats)
        
        return 0
        
    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"ERROR: {e}")
        logger.error("=" * 60)
        cleanup_temp_directories()

        return 1

if __name__ == "__main__":
    sys.exit(main())