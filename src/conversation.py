"""Conversation processing and metadata generation."""

import logging
import re
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Any, Optional, Set
from pathlib import Path

from config import utc_to_faroese, format_faroese_timestamp, get_faroese_date

logger = logging.getLogger(__name__)

def merge_conversations(chat_data: Dict, snap_data: Dict) -> Dict[str, List]:
    """Merge chat and snap histories."""
    logger.info("Merging chat and snap histories")
    merged = {}
    
    # Process chat messages
    for conv_id, messages in chat_data.items():
        if conv_id not in merged:
            merged[conv_id] = []
        for msg in messages:
            msg["Type"] = "message"
        merged[conv_id].extend(messages)
    
    # Process snaps
    for conv_id, snaps in snap_data.items():
        if conv_id not in merged:
            merged[conv_id] = []
        for snap in snaps:
            snap["Type"] = "snap"
        merged[conv_id].extend(snaps)
    
    # Sort by timestamp
    for conv_id in merged:
        merged[conv_id].sort(key=lambda x: int(x.get("Created(microseconds)", 0)))
    
    logger.info(f"Merged {len(merged)} conversations")
    return merged

def determine_account_owner(conversations: Dict[str, List]) -> str:
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

def create_conversation_metadata(conv_id: str, messages: List[Dict],
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
    friends_map = {}
    for friend in friends_json.get("Friends", []):
        friend["friend_status"] = "active"
        friend["friend_list_section"] = "Friends"
        friends_map[friend["Username"]] = friend

    for friend in friends_json.get("Deleted Friends", []):
        friend["friend_status"] = "deleted"
        friend["friend_list_section"] = "Deleted Friends"
        friends_map[friend["Username"]] = friend

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

def convert_message_timestamp(msg: Dict, remove_utc_fields: bool = True) -> Dict:
    """
    Convert message timestamps to Faroese Atlantic Time with millisecond precision.
    Merges 'Created' and 'Created(microseconds)' into single 'Created' field.
    
    Args:
        msg: Message dictionary
        remove_utc_fields: If True, removes Created(microseconds) field
        
    Returns:
        Modified message with Faroese timestamp
    """
    # Get UTC timestamp in milliseconds (field is mislabeled as microseconds)
    utc_timestamp_ms = int(msg.get("Created(microseconds)", 0))
    
    if utc_timestamp_ms > 0:
        # Convert to Faroese time
        faroese_dt = utc_to_faroese(utc_timestamp_ms)
        faroese_timestamp = format_faroese_timestamp(faroese_dt)
        
        # Replace Created field with precise Faroese timestamp
        msg["Created"] = faroese_timestamp
        
        # Optionally remove the old UTC timestamp field
        if remove_utc_fields:
            msg.pop("Created(microseconds)", None)
    
    return msg


def group_messages_by_day(conversations: Dict[str, List[Dict]]) -> Dict[str, Dict[str, List[Dict]]]:
    """
    Group messages by Faroese calendar day.
    
    Args:
        conversations: Dict of {conv_id: [messages]}
        
    Returns:
        Dict of {date: {conv_id: [messages_that_day]}}
    """
    days = defaultdict(lambda: defaultdict(list))
    
    for conv_id, messages in conversations.items():
        for msg in messages:
            # Get UTC timestamp in milliseconds
            utc_timestamp_ms = int(msg.get("Created(microseconds)", 0))
            
            if utc_timestamp_ms > 0:
                # Get Faroese date
                faroese_date = get_faroese_date(utc_timestamp_ms)
                
                # Convert timestamp to Faroese and add to that day
                # Keep Created(microseconds) for now for sorting, remove it later when writing
                msg_copy = msg.copy()
                msg_copy = convert_message_timestamp(msg_copy, remove_utc_fields=False)
                days[faroese_date][conv_id].append(msg_copy)
    
    return dict(days)


def get_conversation_folder_name(metadata: Dict, messages: List[Dict]) -> str:
    """
    Generate folder name for conversation.
    
    Returns:
        - For groups: conversation UUID
        - For individual: recipient username
    """
    is_group = metadata.get("conversation_type") == "group"
    
    if is_group:
        # For groups: use conversation UUID
        return metadata.get("conversation_id", "unknown_group")
    else:
        # For individual conversations: use recipient username
        participants = metadata.get("participants", [])
        if participants:
            # Get the first participant's username (the other person, not account owner)
            return participants[0].get("username") or metadata.get("conversation_id", "unknown_user")
        else:
            return metadata.get("conversation_id", "unknown_user")


def extract_date_from_filename(filename: str) -> Optional[str]:
    """
    Extract date (YYYY-MM-DD) from media filename.
    
    Args:
        filename: Media filename like "2024-01-15_media~..."
        
    Returns:
        Date string or None
    """
    match = re.match(r'(\d{4}-\d{2}-\d{2})', filename)
    if match:
        return match.group(1)
    return None


def generate_index_json(conversations: Dict[str, List[Dict]], friends_json: Dict, account_owner: str, days_data: Dict[str, Dict[str, List[Dict]]]) -> Dict:
    """
    Generate simplified master index.json with users and groups.
    
    Args:
        conversations: Original conversations dict
        friends_json: Friends data
        account_owner: Account owner username
        days_data: Day-grouped messages
        
    Returns:
        Simplified index dictionary with users and groups
    """
    # Build friends map
    friends_map = {}
    for friend in friends_json.get("Friends", []):
        friends_map[friend["Username"]] = friend.get("Display Name", friend["Username"])
    for friend in friends_json.get("Deleted Friends", []):
        friends_map[friend["Username"]] = friend.get("Display Name", friend["Username"])
    
    # Collect all unique users and groups
    all_users = set()
    groups = []
    
    for conv_id, messages in conversations.items():
        if not messages:
            continue
        
        # Check if it's a group
        is_group = any(msg.get("Conversation Title") for msg in messages)
        
        # Collect participants
        participants = set()
        for msg in messages:
            sender = msg.get("From")
            if sender:
                participants.add(sender)
        
        if is_group:
            # Get group name
            group_name = None
            for msg in messages:
                if msg.get("Conversation Title"):
                    group_name = msg["Conversation Title"]
                    break
            
            # Collect members (excluding owner)
            members = sorted([p for p in participants if p != account_owner])
            
            groups.append({
                "group_id": conv_id,
                "name": group_name or conv_id,
                "members": members
            })
            
            # Add all participants to users set
            all_users.update(participants)
        else:
            # Individual conversation - add recipient
            all_users.update(participants)
            all_users.add(conv_id)  # Add conversation ID as it might be the other person
    
    # Remove account owner from users
    all_users.discard(account_owner)
    
    # Build users list with display names
    users = []
    for username in sorted(all_users):
        users.append({
            "username": username,
            "display_name": friends_map.get(username, username)
        })
    
    # Sort groups by name
    groups.sort(key=lambda g: g["name"])
    
    return {
        "account_owner": account_owner,
        "users": users,
        "groups": groups
    }