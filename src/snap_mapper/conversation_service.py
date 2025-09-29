"""Conversation processing and metadata generation service."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set

from .data_models import ConversationMetadata
from .utils import sanitize_filename

logger = logging.getLogger(__name__)


class ConversationService:
    """Service for handling conversation data processing."""
    
    def __init__(self):
        """Initialize the conversation service."""
        pass
        
    def merge_conversations(self, chat_data: Dict, snap_data: Dict) -> Dict[str, List]:
        """Merge chat and snap histories."""
        logger.info("Merging chat and snap histories")
        merged = {}
        
        # Process both data sources using the helper method
        self._process_source(chat_data, "message", merged)
        self._process_source(snap_data, "snap", merged)
        
        # Sort by timestamp
        for conv_id in merged:
            merged[conv_id].sort(key=lambda x: int(x.get("Created(microseconds)", 0)))
        
        logger.info(f"Merged {len(merged)} conversations")
        return merged
    
    def _process_source(self, source_data: Dict, message_type: str, target_dict: Dict) -> None:
        """Process a single data source and add to target dictionary."""
        for conv_id, items in source_data.items():
            if conv_id not in target_dict:
                target_dict[conv_id] = []
            for item in items:
                item["Type"] = message_type
            target_dict[conv_id].extend(items)
    
    def determine_account_owner(self, conversations: Dict[str, List]) -> str:
        """Determine account owner from messages."""
        for messages in conversations.values():
            for msg in messages:
                if msg.get("IsSender"):
                    owner = msg.get("From")
                    if owner:
                        logger.info(f"Determined account owner: {owner}")
                        return owner
        
        logger.warning("Could not determine account owner")
        return "unknown"
    
    def create_conversation_metadata(self, conv_id: str, messages: List[Dict],
                                   friends_json: Dict, owner: str) -> Dict:
        """Create metadata for a conversation."""
        is_group = any(msg.get("Conversation Title") for msg in messages)
        
        # Get participants inline
        participants = set()
        for msg in messages:
            sender = msg.get("From")
            if sender:
                participants.add(sender)
        
        if not is_group:
            participants.add(conv_id)
        participants.discard(owner)
        
        # Process friends inline
        friends_map = self._build_friends_map(friends_json)
        
        # Build participant list
        participants_list = []
        for username in sorted(participants):
            friend = friends_map.get(username, {})
            participants_list.append({
                "username": username,
                "display_name": friend.get("Display Name", username),
                "creation_timestamp": friend.get("Creation Timestamp", "N/A"),
                "last_modified_timestamp": friend.get("Last Modified Timestamp", "N/A"),
                "source": friend.get("Source", "unknown"),
                "friend_status": friend.get("friend_status", "not_found"),
                "friend_list_section": friend.get("friend_list_section", "Not Found"),
                "is_owner": False
            })
        
        # Create metadata
        metadata = {
            "conversation_type": "group" if is_group else "individual",
            "conversation_id": conv_id,
            "total_messages": len(messages),
            "snap_count": sum(1 for msg in messages if msg.get("Type") == "snap"),
            "chat_count": sum(1 for msg in messages if msg.get("Type") == "message"),
            "participants": participants_list,
            "participant_count": len(participants_list),
            "account_owner": owner,
            "date_range": {
                "first_message": messages[0].get("Created", "N/A") if messages else "N/A",
                "last_message": messages[-1].get("Created", "N/A") if messages else "N/A"
            },
            "index_created": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        }
        
        # Add group name if applicable
        if is_group:
            for msg in messages:
                if msg.get("Conversation Title"):
                    metadata["group_name"] = msg["Conversation Title"]
                    break
        
        return metadata
    
    def _build_friends_map(self, friends_json: Dict) -> Dict[str, Dict]:
        """Build a map of username to friend data."""
        friends_map = {}
        
        # Process active friends
        for friend in friends_json.get("Friends", []):
            friend["friend_status"] = "active"
            friend["friend_list_section"] = "Friends"
            friends_map[friend["Username"]] = friend
        
        # Process deleted friends
        for friend in friends_json.get("Deleted Friends", []):
            friend["friend_status"] = "deleted"
            friend["friend_list_section"] = "Deleted Friends"
            friends_map[friend["Username"]] = friend
        
        return friends_map
    
    def get_conversation_folder_name(self, metadata: Dict, messages: List[Dict]) -> str:
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


# Backward compatibility functions - these can be removed after refactoring is complete
def merge_conversations(chat_data: Dict, snap_data: Dict) -> Dict[str, List]:
    """Backward compatibility wrapper."""
    service = ConversationService()
    return service.merge_conversations(chat_data, snap_data)


def determine_account_owner(conversations: Dict[str, List]) -> str:
    """Backward compatibility wrapper."""
    service = ConversationService()
    return service.determine_account_owner(conversations)


def create_conversation_metadata(conv_id: str, messages: List[Dict],
                               friends_json: Dict, owner: str) -> Dict:
    """Backward compatibility wrapper."""
    service = ConversationService()
    return service.create_conversation_metadata(conv_id, messages, friends_json, owner)


def get_conversation_folder_name(metadata: Dict, messages: List[Dict]) -> str:
    """Backward compatibility wrapper."""
    service = ConversationService()
    return service.get_conversation_folder_name(metadata, messages)
