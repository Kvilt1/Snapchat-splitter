"""Central orchestrator for Snapchat Media Mapper processing."""

import logging
import shutil
import time
from pathlib import Path
from typing import Dict, Set, Optional

from .config import INPUT_DIR, OUTPUT_DIR
from .data_models import Stats
from .utils import ensure_directory, load_json
from .media_service import MediaService
from .conversation_service import ConversationService
from .io_writer import ArtifactWriter


logger = logging.getLogger(__name__)


class Processor:
    """Main orchestrator for the Snapchat media mapping process."""
    
    def __init__(self, input_dir: Path = INPUT_DIR, output_dir: Path = OUTPUT_DIR, no_clean: bool = False):
        """Initialize the processor with configuration."""
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir) 
        self.no_clean = no_clean
        self.stats = Stats()
        
        # Initialize services
        self.media_service = MediaService()
        self.conversation_service = ConversationService()
        self.artifact_writer = ArtifactWriter(self.output_dir)
        
        # State variables - initialized during processing
        self.export_dir: Optional[Path] = None
        self.json_dir: Optional[Path] = None
        self.source_media_dir: Optional[Path] = None
        self.temp_merged_dir: Optional[Path] = None
        self.conversations: Dict = {}
        self.account_owner: str = ""
        self.media_index: Dict = {}
        self.mappings: Dict = {}
        self.mapped_files: Set[str] = set()
        self.friends_json: Dict = {}
        
    def run(self) -> int:
        """Execute the complete processing workflow."""
        start_time = time.time()
        
        logger.info("=" * 60)
        logger.info("    SNAPCHAT MEDIA MAPPER - STARTING")
        logger.info("=" * 60)
        
        try:
            self._initialize()
            self._merge_overlays() 
            self._load_and_process_data()
            self._index_and_map_media()
            self._organize_output()
            self._process_orphaned_media()
        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"ERROR: {e}")
            logger.error("=" * 60)
            return 1
        finally:
            self._cleanup()
            
        self._print_summary(time.time() - start_time)
        return 0
        
    def _initialize(self) -> None:
        """Initialize directories and find export folder."""
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: INITIALIZATION")
        logger.info("=" * 60)
        
        # Clean output if requested
        if not self.no_clean and self.output_dir.exists():
            logger.info(f"Cleaning output directory: {self.output_dir}")
            shutil.rmtree(self.output_dir)
            
        # Find export folder
        self.export_dir = self._find_export_folder()
        self.json_dir = self.export_dir / "json"
        self.source_media_dir = self.export_dir / "chat_media"
        
        logger.info(f"Processing export from: {self.export_dir}")
        self.stats.phase_times['initialization'] = time.time() - phase_start
        
    def _merge_overlays(self) -> None:
        """Merge overlay pairs phase."""
        phase_start = time.time()
        
        # Create a temporary directory for merged files (not in output)
        self.temp_merged_dir = self.export_dir.parent / f"temp_merged_{int(time.time())}"
        
        merged_files, merge_stats = self.media_service.merge_overlay_pairs(self.source_media_dir, self.temp_merged_dir)
        self.stats.total_media = merge_stats.get('total_media', 0)
        self.stats.total_overlay = merge_stats.get('total_overlay', 0)
        self.stats.total_merged = merge_stats.get('total_merged', 0)
        self.stats.phase_times['overlay_merging'] = time.time() - phase_start
        
    def _load_and_process_data(self) -> None:
        """Load and process conversation data."""
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: DATA LOADING AND PROCESSING")
        logger.info("=" * 60)
        
        chat_data = load_json(self.json_dir / "chat_history.json")
        snap_data = load_json(self.json_dir / "snap_history.json")
        self.friends_json = load_json(self.json_dir / "friends.json")
        
        if not chat_data and not snap_data:
            raise ValueError("No chat or snap data found")
        
        # Process conversations using service
        self.conversations = self.conversation_service.merge_conversations(chat_data, snap_data)
        self.account_owner = self.conversation_service.determine_account_owner(self.conversations)
        
        logger.info(f"Loaded {len(self.conversations)} conversations")
        self.stats.phase_times['data_loading'] = time.time() - phase_start
        
    def _index_and_map_media(self) -> None:
        """Index media files and map them to messages."""
        phase_start = time.time()
        
        # Index both source media and the merged media subdirectory
        merged_media_dir = self.temp_merged_dir / "merged_media" if self.temp_merged_dir.exists() else None
        self.media_index, index_stats = self.media_service.index_media_files(self.source_media_dir, merged_media_dir)
        
        self.mappings, self.mapped_files, mapping_stats = self.media_service.map_media_to_messages(self.conversations, self.media_index)
        self.stats.mapped_by_id = mapping_stats.get('mapped_by_id', 0)
        self.stats.mapped_by_timestamp = mapping_stats.get('mapped_by_timestamp', 0)
        self.stats.phase_times['media_mapping'] = time.time() - phase_start
        
    def _organize_output(self) -> None:
        """Organize output files into conversation directories."""
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: OUTPUT ORGANIZATION")
        logger.info("=" * 60)
        
        # Setup artifact writer directories
        self.artifact_writer.setup_directories()
        
        conversation_count = 0
        materialized_files = set()
        
        for conv_id, messages in self.conversations.items():
            if not messages:
                continue
                
            # Create metadata using service
            metadata = self.conversation_service.create_conversation_metadata(
                conv_id, messages, self.friends_json, self.account_owner
            )
            
            # Get media mapping for this conversation
            media_mapping = self.mappings.get(conv_id)
            
            # Write conversation using artifact writer
            conv_dir = self.artifact_writer.write_conversation(metadata, messages, media_mapping)
            
            # Track materialized files
            if media_mapping:
                for items in media_mapping.values():
                    for item in items:
                        materialized_files.add(item["media_file"].filename)
            
            conversation_count += 1
            
        logger.info(f"Organized {conversation_count} conversations")
        self.stats.phase_times['output_organization'] = time.time() - phase_start
        
                
    def _process_orphaned_media(self) -> None:
        """Process orphaned media files."""
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: ORPHANED MEDIA PROCESSING")
        logger.info("=" * 60)
        
        # Find orphaned media files
        orphaned_media_files = []
        for media_id, media_file in self.media_index.items():
            if media_file.filename not in self.mapped_files:
                orphaned_media_files.append(media_file)
        
        # Write orphaned files using artifact writer
        orphaned_count = self.artifact_writer.write_orphaned_media(orphaned_media_files)
        
        self.stats.orphaned = orphaned_count
        logger.info(f"Processed {orphaned_count} orphaned media files")
        self.stats.phase_times['orphaned_processing'] = time.time() - phase_start
        
    def _cleanup(self) -> None:
        """Cleanup temporary files and resources."""
        phase_start = time.time()
        logger.info("=" * 60)
        logger.info("PHASE: CLEANUP")
        logger.info("=" * 60)
        
        # Import here to avoid circular imports during cleanup
        from .media_service import cleanup_process_pool
        
        cleanup_process_pool()
        
        # Clean up temporary merged directory
        if self.temp_merged_dir and self.temp_merged_dir.exists():
            logger.info(f"Removing temporary directory: {self.temp_merged_dir}")
            shutil.rmtree(self.temp_merged_dir)
            
        logger.info("Cleanup complete")
        self.stats.phase_times['cleanup'] = time.time() - phase_start
        
    def _find_export_folder(self) -> Path:
        """Find Snapchat export folder."""
        for d in self.input_dir.iterdir():
            if d.is_dir() and (d / "json").exists() and (d / "chat_media").exists():
                return d
                
        raise FileNotFoundError(
            f"No valid Snapchat export found in '{self.input_dir}'. "
            "Place your export folder (e.g., 'mydata') inside 'input' directory."
        )
        
    def _print_summary(self, total_time: float) -> None:
        """Print final processing summary and write summary file."""
        logger.info("=" * 60)
        logger.info("         PROCESSING COMPLETE - SUMMARY")
        logger.info("=" * 60)
        
        # Calculate totals
        total_media_discovered = self.stats.total_media + self.stats.total_overlay
        total_processed = self.stats.total_merged + (len(self.media_index) - self.stats.total_merged)
        total_mapped = self.stats.mapped_by_id + self.stats.mapped_by_timestamp
        
        logger.info(f"Total media files discovered:        {total_media_discovered}")
        logger.info(f"Successfully processed:              {total_processed}")
        if total_media_discovered > 0:
            process_pct = (total_processed / total_media_discovered) * 100
            logger.info(f"  - Processing rate:                 {process_pct:.1f}%")
        logger.info(f"  - Merged with overlays:            {self.stats.total_merged}")
        
        logger.info("")
        logger.info("Mapping Results:")
        logger.info(f"  - Mapped by Media ID:              {self.stats.mapped_by_id}")
        logger.info(f"  - Mapped by timestamp:             {self.stats.mapped_by_timestamp}")
        logger.info(f"  - Total mapped:                    {total_mapped}")
        logger.info(f"  - Orphaned (unmapped):             {self.stats.orphaned}")
        
        if total_processed > 0:
            map_pct = (total_mapped / total_processed) * 100
            logger.info(f"  - Mapping success rate:            {map_pct:.1f}%")
            
        logger.info("")
        logger.info("Processing Time:")
        for phase, duration in self.stats.phase_times.items():
            logger.info(f"  - {phase.replace('_', ' ').title():<30} {duration:.1f}s")
        logger.info(f"  - {'Total':<30} {total_time:.1f}s")
        
        # Write processing summary to file
        summary_stats = {
            'total_media_discovered': total_media_discovered,
            'total_processed': total_processed,
            'processing_rate_percent': (total_processed / total_media_discovered * 100) if total_media_discovered > 0 else 0,
            'merged_with_overlays': self.stats.total_merged,
            'mapped_by_id': self.stats.mapped_by_id,
            'mapped_by_timestamp': self.stats.mapped_by_timestamp,
            'total_mapped': total_mapped,
            'orphaned': self.stats.orphaned,
            'mapping_success_rate_percent': (total_mapped / total_processed * 100) if total_processed > 0 else 0,
            'phase_times': self.stats.phase_times
        }
        
        self.artifact_writer.write_processing_summary(summary_stats, total_time)
        
        logger.info("=" * 60)
        logger.info(f"✓ Check '{self.output_dir}' directory for results")
        logger.info("=" * 60)
