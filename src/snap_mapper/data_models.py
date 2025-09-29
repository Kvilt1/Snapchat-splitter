"""Data models for Snapchat Media Mapper."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class MediaFile:
    """Represents a media file with its metadata."""
    filename: str
    source_path: Path
    media_id: Optional[str] = None
    timestamp: Optional[int] = None
    is_merged: bool = False
    mapping_method: Optional[str] = None


@dataclass
class Stats:
    """Centralized statistics tracking."""
    # Merge stats
    total_media: int = 0
    total_overlay: int = 0
    total_merged: int = 0

    # Mapping stats
    mapped_by_id: int = 0
    mapped_by_timestamp: int = 0
    orphaned: int = 0

    # Timing
    phase_times: Dict[str, float] = field(default_factory=dict)


@dataclass
class ConversationMetadata:
    """Metadata for a conversation."""
    conversation_type: str  # "group" or "individual"
    conversation_id: str
    total_messages: int
    snap_count: int
    chat_count: int
    participants: list
    participant_count: int
    account_owner: str
    date_range: dict
    index_created: str
    group_name: Optional[str] = None
