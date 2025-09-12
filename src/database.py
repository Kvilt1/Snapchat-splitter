"""Database module for efficient data indexing and querying."""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set
from contextlib import contextmanager
import threading

logger = logging.getLogger(__name__)


class MediaDatabase:
    """SQLite database for efficient media and message indexing."""
    
    def __init__(self, db_path: Path = None):
        """Initialize database connection."""
        self.db_path = db_path or Path("/tmp/snapchat_media.db")
        self.local = threading.local()
        self._init_db()
    
    @property
    def conn(self):
        """Get thread-local database connection."""
        if not hasattr(self.local, 'conn'):
            self.local.conn = sqlite3.connect(str(self.db_path))
            self.local.conn.row_factory = sqlite3.Row
            # Enable optimizations
            self.local.conn.execute("PRAGMA journal_mode = WAL")
            self.local.conn.execute("PRAGMA synchronous = NORMAL")
            self.local.conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
            self.local.conn.execute("PRAGMA temp_store = MEMORY")
            self.local.conn.execute("PRAGMA mmap_size = 268435456")  # 256MB memory map
        return self.local.conn
    
    def _init_db(self):
        """Initialize database schema."""
        with self.conn:
            # Messages table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    created_microseconds INTEGER,
                    created_date TEXT,
                    sender TEXT,
                    is_sender BOOLEAN,
                    type TEXT,
                    media_ids TEXT,
                    content TEXT,
                    metadata TEXT,
                    UNIQUE(conversation_id, message_index)
                )
            """)
            
            # Media index table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS media_index (
                    media_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    file_path TEXT,
                    is_grouped BOOLEAN DEFAULT 0,
                    timestamp_ms INTEGER,
                    file_size INTEGER,
                    file_hash TEXT,
                    metadata TEXT
                )
            """)
            
            # Media mappings table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS media_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    mapping_method TEXT,
                    time_diff_seconds REAL,
                    is_grouped BOOLEAN DEFAULT 0,
                    FOREIGN KEY(conversation_id, message_index) 
                        REFERENCES messages(conversation_id, message_index)
                )
            """)
            
            # Conversations metadata table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    conversation_type TEXT,
                    participant_count INTEGER,
                    message_count INTEGER,
                    first_message_date TEXT,
                    last_message_date TEXT,
                    metadata TEXT
                )
            """)
            
            # Friends table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS friends (
                    username TEXT PRIMARY KEY,
                    display_name TEXT,
                    friend_status TEXT,
                    metadata TEXT
                )
            """)
            
            # Create indexes for performance
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conv_id 
                ON messages(conversation_id)
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp 
                ON messages(created_microseconds)
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_timestamp 
                ON media_index(timestamp_ms)
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mappings_conv 
                ON media_mappings(conversation_id, message_index)
            """)
    
    @contextmanager
    def transaction(self):
        """Context manager for database transactions."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
    
    def insert_messages_batch(self, messages: List[Tuple], batch_size: int = 1000):
        """Insert messages in batches for efficiency."""
        with self.transaction():
            for i in range(0, len(messages), batch_size):
                batch = messages[i:i + batch_size]
                self.conn.executemany("""
                    INSERT OR REPLACE INTO messages 
                    (conversation_id, message_index, created_microseconds, 
                     created_date, sender, is_sender, type, media_ids, content, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
    
    def insert_media_index_batch(self, media_items: List[Tuple], batch_size: int = 1000):
        """Insert media index entries in batches."""
        with self.transaction():
            for i in range(0, len(media_items), batch_size):
                batch = media_items[i:i + batch_size]
                self.conn.executemany("""
                    INSERT OR REPLACE INTO media_index 
                    (media_id, filename, file_path, is_grouped, 
                     timestamp_ms, file_size, file_hash, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
    
    def get_message_by_media_id(self, media_id: str) -> Optional[Dict]:
        """Get message containing specific media ID."""
        cursor = self.conn.execute("""
            SELECT conversation_id, message_index, media_ids
            FROM messages 
            WHERE media_ids LIKE '%' || ? || '%'
        """, (media_id,))
        
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    
    def get_messages_by_timestamp_range(self, timestamp_ms: int, 
                                       threshold_ms: int) -> List[Dict]:
        """Get messages within timestamp range."""
        cursor = self.conn.execute("""
            SELECT conversation_id, message_index, created_microseconds
            FROM messages 
            WHERE created_microseconds BETWEEN ? AND ?
            ORDER BY ABS(created_microseconds - ?)
            LIMIT 10
        """, (timestamp_ms - threshold_ms, timestamp_ms + threshold_ms, timestamp_ms))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def get_unmapped_media(self) -> List[Dict]:
        """Get media files not yet mapped to messages."""
        cursor = self.conn.execute("""
            SELECT mi.filename, mi.timestamp_ms, mi.is_grouped
            FROM media_index mi
            LEFT JOIN media_mappings mm ON mi.filename = mm.filename
            WHERE mm.id IS NULL
        """)
        
        return [dict(row) for row in cursor.fetchall()]
    
    def insert_mapping(self, conversation_id: str, message_index: int,
                      filename: str, mapping_method: str,
                      time_diff_seconds: Optional[float] = None,
                      is_grouped: bool = False):
        """Insert a media mapping."""
        with self.transaction():
            self.conn.execute("""
                INSERT INTO media_mappings 
                (conversation_id, message_index, filename, 
                 mapping_method, time_diff_seconds, is_grouped)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (conversation_id, message_index, filename, 
                  mapping_method, time_diff_seconds, is_grouped))
    
    def get_conversation_messages(self, conversation_id: str) -> List[Dict]:
        """Get all messages for a conversation."""
        cursor = self.conn.execute("""
            SELECT * FROM messages 
            WHERE conversation_id = ?
            ORDER BY message_index
        """, (conversation_id,))
        
        messages = []
        for row in cursor:
            msg = dict(row)
            if msg['metadata']:
                msg.update(json.loads(msg['metadata']))
            messages.append(msg)
        return messages
    
    def get_conversation_mappings(self, conversation_id: str) -> Dict[int, List[Dict]]:
        """Get all media mappings for a conversation."""
        cursor = self.conn.execute("""
            SELECT message_index, filename, mapping_method, 
                   time_diff_seconds, is_grouped
            FROM media_mappings 
            WHERE conversation_id = ?
            ORDER BY message_index
        """, (conversation_id,))
        
        mappings = {}
        for row in cursor:
            msg_idx = row['message_index']
            if msg_idx not in mappings:
                mappings[msg_idx] = []
            mappings[msg_idx].append({
                'filename': row['filename'],
                'mapping_method': row['mapping_method'],
                'time_diff_seconds': row['time_diff_seconds'],
                'is_grouped': bool(row['is_grouped'])
            })
        return mappings
    
    def get_all_conversation_ids(self) -> List[str]:
        """Get all conversation IDs."""
        cursor = self.conn.execute("SELECT DISTINCT conversation_id FROM messages")
        return [row[0] for row in cursor.fetchall()]
    
    def close(self):
        """Close database connection."""
        if hasattr(self.local, 'conn'):
            self.local.conn.close()
            delattr(self.local, 'conn')
    
    def cleanup(self):
        """Remove database file."""
        self.close()
        if self.db_path.exists():
            self.db_path.unlink()