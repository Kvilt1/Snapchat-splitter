#!/usr/bin/env python3
"""Optimized main orchestration for Snapchat media mapper - handles 50MB+ JSON and 30GB+ media."""

import argparse
import logging
import shutil
import sys
import gc
import tempfile
from pathlib import Path
from typing import Dict, Set, Optional
import time
import psutil

from config_optimized import (
    INPUT_DIR,
    OUTPUT_DIR,
    TIMESTAMP_THRESHOLD_SECONDS,
    PERFORMANCE_CONFIG,
    ensure_directory,
    load_json_optimized,
    save_json_chunked,
    sanitize_filename,
    logger,
    get_memory_usage,
    check_available_memory,
    ProgressTracker
)

from database import MediaDatabase
from json_streaming import StreamingJSONProcessor, ChunkedJSONWriter
from media_processing_optimized import OptimizedMediaProcessor

from conversation import (
    process_friends_data,
    determine_account_owner,
    create_conversation_metadata,
    get_conversation_folder_name
)


class OptimizedSnapchatProcessor:
    """Main processor with optimizations for large datasets."""
    
    def __init__(self, input_dir: Path, output_dir: Path, no_clean: bool = False):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.no_clean = no_clean
        self.db = None
        self.media_processor = None
        self.json_processor = None
        self.stats = {}
        self.phase_times = {}
    
    def find_export_folder(self) -> Path:
        """Find Snapchat export folder."""
        for d in self.input_dir.iterdir():
            if d.is_dir() and (d / "json").exists() and (d / "chat_media").exists():
                return d
        
        raise FileNotFoundError(
            f"No valid Snapchat export found in '{self.input_dir}'. "
            "Place your export folder (e.g., 'mydata') inside 'input' directory."
        )
    
    def initialize(self):
        """Initialize processors and database."""
        logger.info("=" * 60)
        logger.info("PHASE: INITIALIZATION")
        logger.info("=" * 60)
        
        # Check available memory
        available_mb = check_available_memory()
        logger.info(f"Available memory: {available_mb:.1f}MB")
        logger.info(f"Max memory limit: {PERFORMANCE_CONFIG['max_memory_mb']}MB")
        
        # Clean output if requested
        if not self.no_clean and self.output_dir.exists():
            logger.info(f"Cleaning output directory: {self.output_dir}")
            shutil.rmtree(self.output_dir)
        
        # Initialize database
        if PERFORMANCE_CONFIG['use_database']:
            db_path = self.output_dir / "media_index.db"
            ensure_directory(self.output_dir)
            self.db = MediaDatabase(db_path)
            logger.info(f"Database initialized at {db_path}")
        
        # Initialize processors
        self.media_processor = OptimizedMediaProcessor(
            max_workers=PERFORMANCE_CONFIG['max_workers'],
            batch_size=PERFORMANCE_CONFIG['batch_size'],
            max_memory_mb=PERFORMANCE_CONFIG['max_memory_mb']
        )
        
        self.json_processor = StreamingJSONProcessor(
            chunk_size=PERFORMANCE_CONFIG['json_chunk_size_mb'] * 1024 * 1024
        )
        
        logger.info(f"Initialized with {PERFORMANCE_CONFIG['max_workers']} workers")
    
    def process_json_data_streaming(self, json_dir: Path) -> Dict:
        """Process JSON data using streaming for large files."""
        logger.info("=" * 60)
        logger.info("PHASE: STREAMING JSON DATA PROCESSING")
        logger.info("=" * 60)
        
        stats = {
            'conversations': 0,
            'messages': 0,
            'friends': 0
        }
        
        # Process chat history with streaming
        chat_file = json_dir / "chat_history.json"
        if chat_file.exists():
            file_size_mb = chat_file.stat().st_size / 1024 / 1024
            logger.info(f"Processing chat_history.json ({file_size_mb:.1f}MB)")
            
            if self.db and file_size_mb > PERFORMANCE_CONFIG['json_chunk_size_mb']:
                # Stream directly to database for large files
                def process_chat_batch(conv_id: str, messages: list):
                    message_tuples = []
                    for idx, msg in enumerate(messages):
                        message_tuples.append((
                            conv_id, idx, 
                            int(msg.get("Created(microseconds)", 0)),
                            msg.get("Created", ""),
                            msg.get("From", ""),
                            msg.get("IsSender", False),
                            "message",
                            msg.get("Media IDs", ""),
                            msg.get("Text", ""),
                            None  # metadata
                        ))
                    
                    if message_tuples:
                        self.db.insert_messages_batch(message_tuples)
                    
                    stats['messages'] += len(messages)
                    stats['conversations'] += 1
                
                result = self.json_processor.parse_chat_history_stream(
                    chat_file, process_chat_batch, 
                    batch_size=PERFORMANCE_CONFIG['batch_size']
                )
                logger.info(f"Streamed {result['messages']} messages from {result['conversations']} conversations")
            else:
                # Load normally for smaller files
                chat_data = load_json_optimized(chat_file)
                stats['conversations'] = len(chat_data)
                stats['messages'] = sum(len(msgs) for msgs in chat_data.values())
        
        # Process snap history similarly
        snap_file = json_dir / "snap_history.json"
        if snap_file.exists():
            file_size_mb = snap_file.stat().st_size / 1024 / 1024
            logger.info(f"Processing snap_history.json ({file_size_mb:.1f}MB)")
            
            if self.db and file_size_mb > PERFORMANCE_CONFIG['json_chunk_size_mb']:
                def process_snap_batch(conv_id: str, snaps: list):
                    snap_tuples = []
                    for idx, snap in enumerate(snaps):
                        snap_tuples.append((
                            conv_id, idx + 10000000,  # Offset to avoid conflicts
                            int(snap.get("Created(microseconds)", 0)),
                            snap.get("Created", ""),
                            snap.get("From", ""),
                            snap.get("IsSender", False),
                            "snap",
                            snap.get("Media IDs", ""),
                            "",  # No text in snaps
                            None
                        ))
                    
                    if snap_tuples:
                        self.db.insert_messages_batch(snap_tuples)
                    
                    stats['messages'] += len(snaps)
                
                result = self.json_processor.parse_chat_history_stream(
                    snap_file, process_snap_batch,
                    batch_size=PERFORMANCE_CONFIG['batch_size']
                )
                logger.info(f"Streamed {result['messages']} snaps")
        
        # Process friends data
        friends_file = json_dir / "friends.json"
        if friends_file.exists():
            friends_data = load_json_optimized(friends_file)
            friends_map = process_friends_data(friends_data)
            stats['friends'] = len(friends_map)
            
            # Store in database if available
            if self.db:
                friend_tuples = []
                for username, friend_data in friends_map.items():
                    friend_tuples.append((
                        username,
                        friend_data.get("Display Name", username),
                        friend_data.get("friend_status", "unknown"),
                        None  # metadata
                    ))
                
                if friend_tuples:
                    with self.db.transaction():
                        self.db.conn.executemany("""
                            INSERT OR REPLACE INTO friends 
                            (username, display_name, friend_status, metadata)
                            VALUES (?, ?, ?, ?)
                        """, friend_tuples)
        
        logger.info(f"Processed {stats['conversations']} conversations, "
                   f"{stats['messages']} messages, {stats['friends']} friends")
        
        # Force garbage collection after large JSON processing
        gc.collect()
        
        return stats
    
    def process_media_optimized(self, source_media_dir: Path, temp_media_dir: Path) -> Dict:
        """Process media files with optimizations."""
        logger.info("=" * 60)
        logger.info("PHASE: OPTIMIZED MEDIA PROCESSING")
        logger.info("=" * 60)
        
        # Check media directory size
        total_size = sum(f.stat().st_size for f in source_media_dir.rglob('*') if f.is_file())
        total_size_gb = total_size / 1024 / 1024 / 1024
        logger.info(f"Total media size: {total_size_gb:.2f}GB")
        
        # Merge overlays with optimized processor
        merged_files, merge_stats = self.media_processor.merge_overlay_pairs_optimized(
            source_media_dir, temp_media_dir
        )
        
        # Copy unmerged files in batches
        logger.info("Copying unmerged files in batches...")
        copied_count = self._copy_unmerged_files_batched(
            source_media_dir, temp_media_dir, merged_files
        )
        
        # Index media files
        media_index, index_stats = self.media_processor.index_media_files_optimized(
            temp_media_dir, self.db
        )
        
        stats = {
            'merge': merge_stats,
            'copied_files': copied_count,
            'index': index_stats,
            'total_size_gb': total_size_gb
        }
        
        return stats
    
    def _copy_unmerged_files_batched(self, source_dir: Path, temp_dir: Path, 
                                    merged_files: Set[str]) -> int:
        """Copy unmerged files in batches to manage memory."""
        copied = 0
        skipped_overlays = 0
        batch = []
        batch_size = 100
        
        progress = ProgressTracker(
            sum(1 for _ in source_dir.iterdir() if _.is_file()),
            "Copying unmerged files"
        )
        
        for item in source_dir.iterdir():
            if not item.is_file():
                continue
            
            progress.update()
            
            # Skip merged files, thumbnails, and overlays
            if item.name in merged_files:
                continue
            if "thumbnail" in item.name.lower():
                continue
            if "_overlay~" in item.name:
                skipped_overlays += 1
                continue
            
            batch.append(item)
            
            # Process batch when full
            if len(batch) >= batch_size:
                for file_path in batch:
                    try:
                        shutil.copy(file_path, temp_dir / file_path.name)
                        copied += 1
                    except Exception as e:
                        logger.error(f"Error copying {file_path.name}: {e}")
                
                batch = []
                
                # Check memory usage
                if get_memory_usage() > PERFORMANCE_CONFIG['max_memory_mb'] * 0.8:
                    gc.collect()
        
        # Process remaining files
        for file_path in batch:
            try:
                shutil.copy(file_path, temp_dir / file_path.name)
                copied += 1
            except Exception as e:
                logger.error(f"Error copying {file_path.name}: {e}")
        
        progress.finish()
        
        logger.info(f"Copied {copied} unmerged files")
        if skipped_overlays:
            logger.info(f"Skipped {skipped_overlays} overlay files")
        
        return copied
    
    def map_media_to_messages_optimized(self) -> Dict:
        """Map media to messages using database for efficiency."""
        logger.info("=" * 60)
        logger.info("PHASE: OPTIMIZED MEDIA MAPPING")
        logger.info("=" * 60)
        
        stats = {
            'mapped_by_id': 0,
            'mapped_by_timestamp': 0,
            'unmapped': 0
        }
        
        if not self.db:
            logger.warning("Database not available, skipping optimized mapping")
            return stats
        
        # Phase 1: Map by Media ID using database
        logger.info("Phase 1: Mapping by Media ID...")
        
        cursor = self.db.conn.execute("""
            SELECT m.conversation_id, m.message_index, m.media_ids,
                   mi.filename, mi.is_grouped
            FROM messages m
            JOIN media_index mi ON m.media_ids LIKE '%' || mi.media_id || '%'
            WHERE m.media_ids IS NOT NULL AND m.media_ids != ''
        """)
        
        for row in cursor:
            self.db.insert_mapping(
                row['conversation_id'],
                row['message_index'],
                row['filename'],
                'media_id',
                is_grouped=bool(row['is_grouped'])
            )
            stats['mapped_by_id'] += 1
        
        logger.info(f"Mapped {stats['mapped_by_id']} files by Media ID")
        
        # Phase 2: Map by timestamp
        logger.info("Phase 2: Mapping by timestamp...")
        
        unmapped_media = self.db.get_unmapped_media()
        threshold_ms = TIMESTAMP_THRESHOLD_SECONDS * 1000
        
        progress = ProgressTracker(len(unmapped_media), "Timestamp mapping")
        
        for media in unmapped_media:
            progress.update()
            
            if media['timestamp_ms']:
                messages = self.db.get_messages_by_timestamp_range(
                    media['timestamp_ms'], threshold_ms
                )
                
                if messages:
                    best_match = messages[0]  # Already sorted by time difference
                    time_diff = abs(best_match['created_microseconds'] - media['timestamp_ms'])
                    
                    self.db.insert_mapping(
                        best_match['conversation_id'],
                        best_match['message_index'],
                        media['filename'],
                        'timestamp',
                        time_diff_seconds=time_diff / 1000.0,
                        is_grouped=bool(media['is_grouped'])
                    )
                    stats['mapped_by_timestamp'] += 1
                else:
                    stats['unmapped'] += 1
            else:
                stats['unmapped'] += 1
        
        progress.finish()
        
        logger.info(f"Mapped {stats['mapped_by_timestamp']} files by timestamp")
        logger.info(f"Unmapped files: {stats['unmapped']}")
        
        return stats
    
    def organize_output_optimized(self, temp_media_dir: Path) -> Dict:
        """Organize output with batched operations."""
        logger.info("=" * 60)
        logger.info("PHASE: OPTIMIZED OUTPUT ORGANIZATION")
        logger.info("=" * 60)
        
        stats = {
            'conversations': 0,
            'orphaned': 0
        }
        
        ensure_directory(self.output_dir)
        
        if self.db:
            # Get all conversations from database
            conv_ids = self.db.get_all_conversation_ids()
            
            progress = ProgressTracker(len(conv_ids), "Organizing conversations")
            
            for conv_id in conv_ids:
                progress.update()
                
                # Get messages and mappings from database
                messages = self.db.get_conversation_messages(conv_id)
                if not messages:
                    continue
                
                mappings = self.db.get_conversation_mappings(conv_id)
                
                # Create metadata (need to load friends data)
                # This is kept simple for compatibility
                metadata = {
                    "conversation_id": conv_id,
                    "total_messages": len(messages),
                    "conversation_type": "individual"  # Simplified
                }
                
                # Create output directory
                folder_name = f"{conv_id}"
                folder_name = sanitize_filename(folder_name)
                
                conv_dir = self.output_dir / "conversations" / folder_name
                ensure_directory(conv_dir)
                
                # Process media in batches
                if mappings:
                    media_dir = conv_dir / "media"
                    ensure_directory(media_dir)
                    
                    for msg_idx, items in mappings.items():
                        for item in items:
                            source = temp_media_dir / item['filename']
                            dest = media_dir / item['filename']
                            
                            if source.exists() and not dest.exists():
                                try:
                                    if source.is_file():
                                        shutil.copy(source, dest)
                                    elif source.is_dir():
                                        shutil.copytree(source, dest)
                                except Exception as e:
                                    logger.error(f"Error copying {item['filename']}: {e}")
                
                # Save conversation data efficiently
                save_json_chunked({
                    "conversation_metadata": metadata,
                    "messages": messages[:1000]  # Limit for memory
                }, conv_dir / "conversation.json")
                
                stats['conversations'] += 1
            
            progress.finish()
        
        # Handle orphaned media
        stats['orphaned'] = self._handle_orphaned_media_optimized(temp_media_dir)
        
        logger.info(f"Organized {stats['conversations']} conversations")
        logger.info(f"Processed {stats['orphaned']} orphaned media files")
        
        return stats
    
    def _handle_orphaned_media_optimized(self, temp_dir: Path) -> int:
        """Handle orphaned media with batching."""
        orphaned_dir = self.output_dir / "orphaned"
        ensure_directory(orphaned_dir)
        
        orphaned_count = 0
        
        # Get mapped files from database
        mapped_files = set()
        if self.db:
            cursor = self.db.conn.execute("SELECT DISTINCT filename FROM media_mappings")
            mapped_files = {row[0] for row in cursor}
        
        # Process orphaned files in batches
        batch = []
        batch_size = 50
        
        for item in temp_dir.iterdir():
            # Skip mapped files, thumbnails, and overlays
            if item.name in mapped_files:
                continue
            if "thumbnail" in item.name.lower():
                continue
            if "_overlay~" in item.name:
                continue
            
            batch.append(item)
            
            if len(batch) >= batch_size:
                for file_path in batch:
                    try:
                        if file_path.is_file():
                            shutil.copy(file_path, orphaned_dir / file_path.name)
                        elif file_path.is_dir():
                            shutil.copytree(file_path, orphaned_dir / file_path.name)
                        orphaned_count += 1
                    except Exception as e:
                        logger.error(f"Error copying orphaned {file_path.name}: {e}")
                
                batch = []
                gc.collect()  # Force garbage collection
        
        # Process remaining files
        for file_path in batch:
            try:
                if file_path.is_file():
                    shutil.copy(file_path, orphaned_dir / file_path.name)
                elif file_path.is_dir():
                    shutil.copytree(file_path, orphaned_dir / file_path.name)
                orphaned_count += 1
            except Exception as e:
                logger.error(f"Error copying orphaned {file_path.name}: {e}")
        
        return orphaned_count
    
    def cleanup(self, temp_media_dir: Path):
        """Clean up temporary files and close resources."""
        logger.info("=" * 60)
        logger.info("PHASE: CLEANUP")
        logger.info("=" * 60)
        
        # Close database
        if self.db:
            self.db.close()
            if not PERFORMANCE_CONFIG['cleanup_temp']:
                logger.info("Keeping database for debugging")
            else:
                self.db.cleanup()
        
        # Remove temporary directory
        if temp_media_dir.exists():
            if PERFORMANCE_CONFIG['cleanup_temp']:
                shutil.rmtree(temp_media_dir)
                logger.info("Removed temporary directory")
            else:
                logger.info(f"Keeping temporary directory for debugging: {temp_media_dir}")
        
        # Force final garbage collection
        gc.collect()
    
    def run(self):
        """Run the optimized processing pipeline."""
        start_time = time.time()
        
        logger.info("=" * 60)
        logger.info("    OPTIMIZED SNAPCHAT MEDIA MAPPER - STARTING")
        logger.info("=" * 60)
        logger.info(f"Configuration:")
        logger.info(f"  - Max workers: {PERFORMANCE_CONFIG['max_workers']}")
        logger.info(f"  - Max memory: {PERFORMANCE_CONFIG['max_memory_mb']}MB")
        logger.info(f"  - Batch size: {PERFORMANCE_CONFIG['batch_size']}")
        logger.info(f"  - Using database: {PERFORMANCE_CONFIG['use_database']}")
        
        try:
            # INITIALIZATION
            phase_start = time.time()
            self.initialize()
            self.phase_times['initialization'] = time.time() - phase_start
            
            # Find export folder
            export_dir = self.find_export_folder()
            json_dir = export_dir / "json"
            source_media_dir = export_dir / "chat_media"
            temp_media_dir = self.output_dir / "temp_media"
            
            logger.info(f"Processing export from: {export_dir}")
            
            # JSON PROCESSING
            phase_start = time.time()
            json_stats = self.process_json_data_streaming(json_dir)
            self.stats['json'] = json_stats
            self.phase_times['json_processing'] = time.time() - phase_start
            
            # MEDIA PROCESSING
            phase_start = time.time()
            media_stats = self.process_media_optimized(source_media_dir, temp_media_dir)
            self.stats.update(media_stats)
            self.phase_times['media_processing'] = time.time() - phase_start
            
            # MEDIA MAPPING
            phase_start = time.time()
            mapping_stats = self.map_media_to_messages_optimized()
            self.stats['mapping'] = mapping_stats
            self.phase_times['media_mapping'] = time.time() - phase_start
            
            # OUTPUT ORGANIZATION
            phase_start = time.time()
            output_stats = self.organize_output_optimized(temp_media_dir)
            self.stats['output'] = output_stats
            self.phase_times['output_organization'] = time.time() - phase_start
            
            # CLEANUP
            phase_start = time.time()
            self.cleanup(temp_media_dir)
            self.phase_times['cleanup'] = time.time() - phase_start
            
            # FINAL SUMMARY
            total_time = time.time() - start_time
            self.print_summary(total_time)
            
            return 0
            
        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"ERROR: {e}")
            logger.error("=" * 60)
            import traceback
            traceback.print_exc()
            return 1
    
    def print_summary(self, total_time: float):
        """Print processing summary."""
        logger.info("=" * 60)
        logger.info("         OPTIMIZED PROCESSING COMPLETE - SUMMARY")
        logger.info("=" * 60)
        
        # Memory usage
        final_memory = get_memory_usage()
        logger.info(f"Final memory usage: {final_memory:.1f}MB")
        
        # Processing statistics
        if 'json' in self.stats:
            logger.info(f"JSON Processing:")
            logger.info(f"  - Conversations: {self.stats['json'].get('conversations', 0)}")
            logger.info(f"  - Messages: {self.stats['json'].get('messages', 0)}")
            logger.info(f"  - Friends: {self.stats['json'].get('friends', 0)}")
        
        if 'merge' in self.stats:
            logger.info(f"Media Processing:")
            logger.info(f"  - Total size: {self.stats.get('total_size_gb', 0):.2f}GB")
            logger.info(f"  - Merged files: {self.stats['merge'].get('total_merged', 0)}")
            logger.info(f"  - Copied files: {self.stats.get('copied_files', 0)}")
        
        if 'mapping' in self.stats:
            logger.info(f"Mapping Results:")
            logger.info(f"  - Mapped by ID: {self.stats['mapping'].get('mapped_by_id', 0)}")
            logger.info(f"  - Mapped by timestamp: {self.stats['mapping'].get('mapped_by_timestamp', 0)}")
            logger.info(f"  - Unmapped: {self.stats['mapping'].get('unmapped', 0)}")
        
        if 'output' in self.stats:
            logger.info(f"Output Organization:")
            logger.info(f"  - Conversations: {self.stats['output'].get('conversations', 0)}")
            logger.info(f"  - Orphaned files: {self.stats['output'].get('orphaned', 0)}")
        
        logger.info("")
        logger.info("Processing Time:")
        for phase, duration in self.phase_times.items():
            logger.info(f"  - {phase.replace('_', ' ').title():<30} {duration:.1f}s")
        logger.info(f"  - {'Total':<30} {total_time:.1f}s")
        
        logger.info("=" * 60)
        logger.info(f"✓ Check '{self.output_dir}' directory for results")
        logger.info("=" * 60)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Optimized Snapchat export processor")
    parser.add_argument("--input", type=Path, default=INPUT_DIR, help="Input directory")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--no-clean", action="store_true", help="Don't clean output directory")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    parser.add_argument("--max-workers", type=int, help="Override max workers")
    parser.add_argument("--max-memory", type=int, help="Override max memory (MB)")
    parser.add_argument("--no-database", action="store_true", help="Disable database usage")
    args = parser.parse_args()
    
    # Override configuration if provided
    if args.max_workers:
        PERFORMANCE_CONFIG['max_workers'] = args.max_workers
    if args.max_memory:
        PERFORMANCE_CONFIG['max_memory_mb'] = args.max_memory
    if args.no_database:
        PERFORMANCE_CONFIG['use_database'] = False
    
    # Setup logging
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    
    # Run processor
    processor = OptimizedSnapchatProcessor(args.input, args.output, args.no_clean)
    return processor.run()


if __name__ == "__main__":
    sys.exit(main())