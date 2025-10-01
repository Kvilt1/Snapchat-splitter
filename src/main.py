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
from typing import Dict, Set
from collections import defaultdict

from config import (
    INPUT_DIR,
    OUTPUT_DIR,
    ensure_directory,
    load_json,
    save_json,
    sanitize_filename,
    safe_materialize,
    Stats,
    logger
)
from tqdm import tqdm

from media_processing import (
    merge_overlay_pairs,
    index_media_files,
    map_media_to_messages,
    cleanup_process_pool
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
    
    cleanup_process_pool()
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

def process_conversation_media(conv_id: str, messages: list, mapping: dict,
                             conv_dir: Path) -> None:
    """Materialize media files for a conversation on demand."""
    if not mapping:
        return

    media_dir = conv_dir / "media"
    ensure_directory(media_dir)

    for msg_idx, items in mapping.items():
        if msg_idx >= len(messages):
            continue

        media_locations = []
        matched_files = []

        for item in items:
            media_file = item["media_file"]
            dest = media_dir / media_file.filename

            # Handle single file
            if safe_materialize(media_file.source_path, dest):
                matched_files.append(media_file.filename)
            location = f"media/{media_file.filename}"
            media_locations.append(location)

        # Update message
        messages[msg_idx]["media_locations"] = media_locations
        messages[msg_idx]["matched_media_files"] = matched_files
        messages[msg_idx]["is_grouped"] = False  # All files are now individual
        messages[msg_idx]["mapping_method"] = items[0]["mapping_method"]

        if "time_diff_seconds" in items[0]:
            messages[msg_idx]["time_diff_seconds"] = items[0]["time_diff_seconds"]


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
        logger.info("=" * 60)
        logger.info("PHASE: INITIALIZATION")
        logger.info("=" * 60)

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
        logger.info("=" * 60)
        logger.info("PHASE: DATA LOADING AND PROCESSING")
        logger.info("=" * 60)

        # Load JSON files with progress indication
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

        mappings, mapped_files, mapping_stats = map_media_to_messages(conversations, media_index)
        stats.mapped_by_id = mapping_stats.get('mapped_by_id', 0)
        stats.mapped_by_timestamp = mapping_stats.get('mapped_by_timestamp', 0)
        stats.phase_times['media_mapping'] = time.time() - phase_start

        # DAY-BASED OUTPUT ORGANIZATION PHASE
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: DAY-BASED OUTPUT ORGANIZATION")
        logger.info("=" * 60)

        ensure_directory(args.output)

        # Group messages by Faroese calendar day
        logger.info("Grouping messages by Faroese calendar day...")
        days_data = group_messages_by_day(conversations)
        all_days = sorted(days_data.keys())
        
        logger.info(f"Found activity across {len(all_days)} days ({all_days[0]} to {all_days[-1]})")

        # Generate index.json with all conversation metadata
        logger.info("Generating index.json...")
        index_json = generate_index_json(conversations, friends_json, account_owner, days_data)
        save_json(index_json, args.output / "index.json")
        logger.info(f"Generated index.json with {len(index_json['users'])} users and {len(index_json['groups'])} groups")

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

        # Build timestamp index for faster lookup (handles duplicate timestamps)
        logger.info("Building timestamp index for media mapping...")
        timestamp_to_index = {}
        for conv_id, all_messages in conversations.items():
            timestamp_to_index[conv_id] = {}
            for orig_idx, orig_msg in enumerate(all_messages):
                orig_ts = orig_msg.get("Created(microseconds)", 0)
                # Store list of indices for each timestamp to handle duplicates
                if orig_ts not in timestamp_to_index[conv_id]:
                    timestamp_to_index[conv_id][orig_ts] = []
                timestamp_to_index[conv_id][orig_ts].append(orig_idx)
        
        # Calculate total mapped media before splitting
        total_mapped_items_before = sum(
            len(msg_mappings) 
            for conv_mappings in mappings.values() 
            for msg_mappings in conv_mappings.values()
        )
        logger.info(f"Total mapped media items before day splitting: {total_mapped_items_before}")

        # Process each day and track mapping preservation
        total_mapped_items_after = 0
        messages_with_media = 0
        
        with tqdm(total=len(all_days), desc="Processing days", unit="days") as day_pbar:
            for day in all_days:
                day_dir = days_dir / day
                ensure_directory(day_dir)
                
                # Create conversations subfolder
                conversations_dir = day_dir / "conversations"
                ensure_directory(conversations_dir)

                # Process conversations for this day
                day_conversations = days_data[day]
                
                for conv_id, day_messages in day_conversations.items():
                    # Use cached metadata and folder name
                    folder_name = conv_folder_names[conv_id]
                    
                    # Create conversation folder for this day inside conversations/
                    conv_dir = conversations_dir / folder_name
                    ensure_directory(conv_dir)
                    
                    # Find media for this day's messages
                    day_media_dir = conv_dir / "media"
                    
                    if conv_id in mappings:
                        # Use timestamp index for O(1) lookup instead of O(n*m)
                        ts_index = timestamp_to_index.get(conv_id, {})
                        
                        for day_msg in day_messages:
                            day_ts = day_msg.get("Created(microseconds)", 0)
                            orig_indices = ts_index.get(day_ts, [])
                            
                            # Check all original message indices with this timestamp
                            for orig_idx in orig_indices:
                                if orig_idx in mappings[conv_id]:
                                    items = mappings[conv_id][orig_idx]
                                    # Materialize media
                                    if not day_media_dir.exists():
                                        ensure_directory(day_media_dir)
                                    
                                    media_locations = []
                                    matched_files = []
                                    
                                    for item in items:
                                        media_file = item["media_file"]
                                        dest = day_media_dir / media_file.filename
                                        
                                        if safe_materialize(media_file.source_path, dest):
                                            matched_files.append(media_file.filename)
                                        location = f"media/{media_file.filename}"
                                        media_locations.append(location)
                                    
                                    # Update day message (last one wins if multiple matches)
                                    day_msg["media_locations"] = media_locations
                                    day_msg["matched_media_files"] = matched_files
                                    day_msg["is_grouped"] = False
                                    day_msg["mapping_method"] = items[0]["mapping_method"]
                                    
                                    if "time_diff_seconds" in items[0]:
                                        day_msg["time_diff_seconds"] = items[0]["time_diff_seconds"]
                                    
                                    # Track statistics
                                    total_mapped_items_after += len(items)
                                    messages_with_media += 1
                                    break  # Only apply first matching index for this timestamp
                    
                    # Clean up Created(microseconds) from messages before saving
                    clean_messages = []
                    for msg in day_messages:
                        msg_copy = msg.copy()
                        msg_copy.pop("Created(microseconds)", None)
                        clean_messages.append(msg_copy)
                    
                    # Save this day's conversation data
                    save_json({
                        "messages": clean_messages
                    }, conv_dir / "conversation.json")
                
                # Handle orphaned media for this day
                if day in orphaned_by_day:
                    orphaned_dir = day_dir / "orphaned"
                    ensure_directory(orphaned_dir)
                    
                    for media_file in orphaned_by_day[day]:
                        safe_materialize(media_file.source_path, orphaned_dir / media_file.filename)
                
                day_pbar.update(1)

        total_orphaned = sum(len(files) for files in orphaned_by_day.values())
        logger.info(f"Organized {len(all_days)} days with {total_orphaned} orphaned media files")
        logger.info(f"Mapped media preservation: {total_mapped_items_before} items before -> {total_mapped_items_after} items after")
        logger.info(f"Total messages with media in output: {messages_with_media}")
        
        if total_mapped_items_before != total_mapped_items_after:
            logger.warning(f"Mapping mismatch detected! Lost {total_mapped_items_before - total_mapped_items_after} media items during day splitting")
        
        stats.phase_times['output_organization'] = time.time() - phase_start
        stats.orphaned = total_orphaned

        # CLEANUP PHASE
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: CLEANUP")
        logger.info("=" * 60)

        cleanup_process_pool()
        cleanup_temp_directories()

        logger.info("Cleanup complete")
        stats.phase_times['cleanup'] = time.time() - phase_start

        # FINAL SUMMARY
        total_time = time.time() - start_time

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
        logger.info(f"  - Total mapped:                    {total_mapped}")
        logger.info(f"  - Orphaned (unmapped):             {stats.orphaned}")

        if total_processed > 0:
            map_pct = (total_mapped / total_processed) * 100
            logger.info(f"  - Mapping success rate:            {map_pct:.1f}%")

        logger.info("")
        logger.info("Processing Time:")
        for phase, duration in stats.phase_times.items():
            logger.info(f"  - {phase.replace('_', ' ').title():<30} {duration:.1f}s")
        logger.info(f"  - {'Total':<30} {total_time:.1f}s")
        
        logger.info("=" * 60)
        logger.info(f"✓ Check '{args.output}' directory for results")
        logger.info("=" * 60)
        
        return 0
        
    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"ERROR: {e}")
        logger.error("=" * 60)
        cleanup_process_pool()
        cleanup_temp_directories()

        return 1

if __name__ == "__main__":
    sys.exit(main())