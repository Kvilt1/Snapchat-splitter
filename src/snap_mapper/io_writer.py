"""I/O operations service for writing artifacts and managing file system operations."""

import logging
from pathlib import Path
from typing import Dict, Any, List

from .utils import ensure_directory, save_json, safe_materialize, sanitize_filename

logger = logging.getLogger(__name__)


class ArtifactWriter:
    """Service for handling all file system write operations."""
    
    def __init__(self, output_dir: Path):
        """Initialize the artifact writer with output directory."""
        self.output_dir = Path(output_dir)
        self.conversations_dir = self.output_dir / "conversations"
        self.groups_dir = self.output_dir / "groups"
        self.orphaned_dir = self.output_dir / "orphaned"
        
    def setup_directories(self) -> None:
        """Setup the base directory structure."""
        ensure_directory(self.output_dir)
        ensure_directory(self.conversations_dir)
        ensure_directory(self.groups_dir)
        
    def write_conversation(self, metadata: Dict, messages: List[Dict], 
                          media_mapping: Dict = None) -> Path:
        """
        Write a complete conversation with its metadata, messages and media.
        
        Args:
            metadata: Conversation metadata
            messages: List of conversation messages
            media_mapping: Optional mapping of media files to messages
            
        Returns:
            Path to the created conversation directory
        """
        # Determine output directory based on conversation type
        is_group = metadata.get("conversation_type") == "group"
        base_dir = self.groups_dir if is_group else self.conversations_dir
        
        # Create folder name
        folder_name = self._get_conversation_folder_name(metadata, messages)
        folder_name = sanitize_filename(folder_name)
        
        conv_dir = base_dir / folder_name
        ensure_directory(conv_dir)
        
        # Write media files if provided
        if media_mapping:
            self._write_conversation_media(conv_dir, messages, media_mapping)
            
        # Write conversation JSON
        conversation_data = {
            "conversation_metadata": metadata,
            "messages": messages
        }
        save_json(conversation_data, conv_dir / "conversation.json")
        
        return conv_dir
    
    def write_orphaned_media(self, media_files: List) -> int:
        """
        Write orphaned media files to the orphaned directory.
        
        Args:
            media_files: List of MediaFile objects to write as orphaned
            
        Returns:
            Number of successfully written orphaned files
        """
        if not media_files:
            return 0
            
        ensure_directory(self.orphaned_dir)
        orphaned_count = 0
        
        for media_file in media_files:
            # Skip thumbnails and overlays
            if "thumbnail" in media_file.filename.lower() or "_overlay~" in media_file.filename:
                continue
                
            if safe_materialize(media_file.source_path, self.orphaned_dir / media_file.filename):
                orphaned_count += 1
                
        logger.info(f"Wrote {orphaned_count} orphaned media files")
        return orphaned_count
    
    def write_processing_summary(self, stats: Dict, processing_time: float) -> None:
        """
        Write a processing summary file.
        
        Args:
            stats: Processing statistics
            processing_time: Total processing time in seconds
        """
        summary = {
            "processing_summary": {
                "total_processing_time_seconds": processing_time,
                "statistics": stats,
                "output_structure": {
                    "conversations": str(self.conversations_dir),
                    "groups": str(self.groups_dir),
                    "orphaned": str(self.orphaned_dir)
                }
            }
        }
        
        save_json(summary, self.output_dir / "processing_summary.json")
        logger.info("Processing summary written to processing_summary.json")
    
    def _write_conversation_media(self, conv_dir: Path, messages: List[Dict], 
                                 media_mapping: Dict) -> None:
        """Write media files for a conversation."""
        media_dir = conv_dir / "media"
        ensure_directory(media_dir)
        
        for msg_idx, items in media_mapping.items():
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
            
            # Update message with media information
            messages[msg_idx]["media_locations"] = media_locations
            messages[msg_idx]["matched_media_files"] = matched_files
            messages[msg_idx]["is_grouped"] = False  # All files are now individual
            messages[msg_idx]["mapping_method"] = items[0]["mapping_method"]
            
            if "time_diff_seconds" in items[0]:
                messages[msg_idx]["time_diff_seconds"] = items[0]["time_diff_seconds"]
    
    def _get_conversation_folder_name(self, metadata: Dict, messages: List[Dict]) -> str:
        """Generate folder name for conversation."""
        # Get last message date
        last_date = messages[-1].get("Created", "0000-00-00").split(" ")[0] if messages else "0000-00-00"
        
        # Determine base name
        base_name = metadata.get("group_name")
        if not base_name:
            participants = metadata.get("participants", [])
            if participants:
                base_name = (participants[0].get("display_name") or
                           participants[0].get("username"))
            else:
                base_name = metadata.get("conversation_id", "unknown")
        
        return f"{last_date} - {base_name}"
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about created artifacts."""
        stats = {
            "output_directory": str(self.output_dir),
            "conversations_created": 0,
            "groups_created": 0,
            "orphaned_files": 0
        }
        
        if self.conversations_dir.exists():
            stats["conversations_created"] = len(list(self.conversations_dir.iterdir()))
            
        if self.groups_dir.exists():
            stats["groups_created"] = len(list(self.groups_dir.iterdir()))
            
        if self.orphaned_dir.exists():
            stats["orphaned_files"] = len(list(self.orphaned_dir.iterdir()))
            
        return stats
