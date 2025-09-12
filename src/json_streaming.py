"""Streaming JSON processor for handling large files efficiently."""

import json
import logging
from pathlib import Path
from typing import Iterator, Dict, Any, Optional, Callable
import ijson
import mmap
import os

logger = logging.getLogger(__name__)


class StreamingJSONProcessor:
    """Process large JSON files using streaming to minimize memory usage."""
    
    def __init__(self, chunk_size: int = 1024 * 1024):  # 1MB chunks
        self.chunk_size = chunk_size
    
    def parse_chat_history_stream(self, file_path: Path, 
                                 callback: Callable[[str, list], None],
                                 batch_size: int = 100) -> Dict[str, int]:
        """
        Stream parse chat history JSON file.
        Calls callback with conversation_id and batch of messages.
        Returns statistics.
        """
        stats = {
            'conversations': 0,
            'messages': 0,
            'errors': 0
        }
        
        try:
            # Try using ijson for true streaming if available
            return self._parse_with_ijson(file_path, callback, batch_size, stats)
        except ImportError:
            # Fallback to memory-mapped file approach
            logger.info("ijson not available, using memory-mapped file approach")
            return self._parse_with_mmap(file_path, callback, batch_size, stats)
    
    def _parse_with_ijson(self, file_path: Path, callback: Callable,
                         batch_size: int, stats: Dict) -> Dict:
        """Parse using ijson library for true streaming."""
        try:
            import ijson
        except ImportError:
            raise ImportError("ijson not installed")
        
        with open(file_path, 'rb') as file:
            parser = ijson.items(file, 'item')
            
            current_conv_id = None
            message_batch = []
            
            for item in parser:
                if isinstance(item, dict):
                    # Assuming top-level dict with conversation IDs as keys
                    for conv_id, messages in item.items():
                        if conv_id != current_conv_id:
                            # Process previous batch
                            if current_conv_id and message_batch:
                                callback(current_conv_id, message_batch)
                                stats['messages'] += len(message_batch)
                            
                            current_conv_id = conv_id
                            message_batch = []
                            stats['conversations'] += 1
                        
                        # Process messages in batches
                        if isinstance(messages, list):
                            for i in range(0, len(messages), batch_size):
                                batch = messages[i:i + batch_size]
                                callback(conv_id, batch)
                                stats['messages'] += len(batch)
            
            # Process final batch
            if current_conv_id and message_batch:
                callback(current_conv_id, message_batch)
                stats['messages'] += len(message_batch)
        
        return stats
    
    def _parse_with_mmap(self, file_path: Path, callback: Callable,
                        batch_size: int, stats: Dict) -> Dict:
        """Parse using memory-mapped file for efficient memory usage."""
        file_size = file_path.stat().st_size
        
        with open(file_path, 'r+b') as file:
            # Memory map the file
            with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mmapped_file:
                # Read in chunks and parse
                data = mmapped_file.read().decode('utf-8')
                
                try:
                    # Parse the entire JSON (but memory-mapped)
                    json_data = json.loads(data)
                    
                    # Process each conversation
                    for conv_id, messages in json_data.items():
                        stats['conversations'] += 1
                        
                        # Process messages in batches
                        if isinstance(messages, list):
                            for i in range(0, len(messages), batch_size):
                                batch = messages[i:i + batch_size]
                                callback(conv_id, batch)
                                stats['messages'] += len(batch)
                        else:
                            logger.warning(f"Unexpected data type for conversation {conv_id}")
                            stats['errors'] += 1
                
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}")
                    stats['errors'] += 1
                    
                    # Try to recover by processing line by line
                    return self._parse_line_by_line(file_path, callback, batch_size, stats)
        
        return stats
    
    def _parse_line_by_line(self, file_path: Path, callback: Callable,
                           batch_size: int, stats: Dict) -> Dict:
        """Fallback parser that processes JSON line by line."""
        current_conv_id = None
        message_batch = []
        
        with open(file_path, 'r', encoding='utf-8') as file:
            # Try to parse as JSONL (JSON Lines)
            for line_num, line in enumerate(file, 1):
                line = line.strip()
                if not line:
                    continue
                
                try:
                    data = json.loads(line)
                    
                    if isinstance(data, dict):
                        for conv_id, messages in data.items():
                            stats['conversations'] += 1
                            
                            if isinstance(messages, list):
                                for i in range(0, len(messages), batch_size):
                                    batch = messages[i:i + batch_size]
                                    callback(conv_id, batch)
                                    stats['messages'] += len(batch)
                
                except json.JSONDecodeError:
                    # Skip malformed lines
                    stats['errors'] += 1
                    if stats['errors'] <= 10:  # Log first 10 errors
                        logger.warning(f"Skipping malformed JSON at line {line_num}")
        
        return stats
    
    def stream_process_json_file(self, file_path: Path, 
                                processor: Callable[[Dict], None]) -> int:
        """
        Generic streaming processor for any JSON file.
        Processes file in chunks to minimize memory usage.
        """
        processed_count = 0
        
        try:
            # Check file size
            file_size = file_path.stat().st_size
            
            if file_size < 10 * 1024 * 1024:  # Less than 10MB
                # Small file, load normally
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    processor(data)
                    processed_count = 1
            else:
                # Large file, use streaming
                logger.info(f"Processing large file ({file_size / 1024 / 1024:.1f}MB) with streaming")
                
                with open(file_path, 'rb') as file:
                    # Use memory mapping for efficient reading
                    with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mmapped_file:
                        data = mmapped_file.read().decode('utf-8')
                        json_data = json.loads(data)
                        processor(json_data)
                        processed_count = 1
        
        except Exception as e:
            logger.error(f"Error processing JSON file {file_path}: {e}")
            raise
        
        return processed_count


class ChunkedJSONWriter:
    """Write large JSON data in chunks to avoid memory issues."""
    
    def __init__(self, file_path: Path, indent: int = 2):
        self.file_path = file_path
        self.indent = indent
        self.file = None
        self.first_item = True
    
    def __enter__(self):
        self.file = open(self.file_path, 'w', encoding='utf-8')
        self.file.write('{\n')
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.file:
            self.file.write('\n}')
            self.file.close()
    
    def write_item(self, key: str, value: Any):
        """Write a single key-value pair to the JSON file."""
        if not self.first_item:
            self.file.write(',\n')
        else:
            self.first_item = False
        
        # Write key
        self.file.write(f'  {json.dumps(key)}: ')
        
        # Write value
        if isinstance(value, (dict, list)):
            json_str = json.dumps(value, indent=self.indent, ensure_ascii=False)
            # Indent the value properly
            lines = json_str.split('\n')
            self.file.write(lines[0])
            for line in lines[1:]:
                self.file.write('\n  ' + line)
        else:
            self.file.write(json.dumps(value, ensure_ascii=False))
    
    def write_batch(self, items: Dict[str, Any]):
        """Write multiple items at once."""
        for key, value in items.items():
            self.write_item(key, value)