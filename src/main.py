#!/usr/bin/env python3
"""Main orchestration for Snapchat media mapper."""

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Set

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
    get_conversation_folder_name
)

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

        chat_data = load_json(json_dir / "chat_history.json")
        snap_data = load_json(json_dir / "snap_history.json")
        friends_json = load_json(json_dir / "friends.json")

        if not chat_data and not snap_data:
            raise ValueError("No chat or snap data found")

        # Process conversations
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

        # OUTPUT ORGANIZATION PHASE
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: OUTPUT ORGANIZATION")
        logger.info("=" * 60)

        ensure_directory(args.output)

        conversation_count = 0
        materialized_files = set()

        for conv_id, messages in conversations.items():
            if not messages:
                continue

            # Create metadata
            metadata = create_conversation_metadata(conv_id, messages, friends_json, account_owner)

            # Create output directory
            folder_name = get_conversation_folder_name(metadata, messages)
            folder_name = sanitize_filename(folder_name)

            is_group = metadata["conversation_type"] == "group"
            base_dir = args.output / "groups" if is_group else args.output / "conversations"
            conv_dir = base_dir / folder_name
            ensure_directory(conv_dir)

            # Process media
            if conv_id in mappings:
                process_conversation_media(
                    conv_id, messages, mappings[conv_id],
                    conv_dir
                )
                # Track materialized files
                for items in mappings[conv_id].values():
                    for item in items:
                        materialized_files.add(item["media_file"].filename)

            # Save conversation data
            save_json({
                "conversation_metadata": metadata,
                "messages": messages
            }, conv_dir / "conversation.json")

            conversation_count += 1

        logger.info(f"Organized {conversation_count} conversations")
        stats.phase_times['output_organization'] = time.time() - phase_start

        # ORPHANED MEDIA PHASE
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: ORPHANED MEDIA PROCESSING")
        logger.info("=" * 60)

        orphaned_dir = args.output / "orphaned"
        orphaned_count = 0

        # Find and materialize orphaned files
        for media_id, media_file in media_index.items():
            if media_file.filename not in mapped_files:
                # Skip thumbnails and overlays
                if "thumbnail" in media_file.filename.lower() or "_overlay~" in media_file.filename:
                    continue

                ensure_directory(orphaned_dir)
                if safe_materialize(media_file.source_path, orphaned_dir / media_file.filename):
                    orphaned_count += 1

        stats.orphaned = orphaned_count
        logger.info(f"Processed {orphaned_count} orphaned media files")
        stats.phase_times['orphaned_processing'] = time.time() - phase_start

        # CLEANUP PHASE
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: CLEANUP")
        logger.info("=" * 60)

        cleanup_process_pool()

        # Clean up temporary merged directory
        if temp_merged_dir.exists():
            logger.info(f"Removing temporary directory: {temp_merged_dir}")
            shutil.rmtree(temp_merged_dir)

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

        # Clean up temporary merged directory on error
        if 'temp_merged_dir' in locals() and temp_merged_dir.exists():
            logger.info(f"Cleaning up temporary directory: {temp_merged_dir}")
            shutil.rmtree(temp_merged_dir)

        return 1

if __name__ == "__main__":
    sys.exit(main())