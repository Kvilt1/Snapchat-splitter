#!/usr/bin/env python3
"""Main entry point for Snapchat Media Mapper - Refactored Version."""

import argparse
import logging
import sys
from pathlib import Path

from snap_mapper.config import INPUT_DIR, OUTPUT_DIR, LOG_FORMAT, DEFAULT_LOG_LEVEL
from snap_mapper.processing import Processor


def setup_logging(level: str = "INFO") -> None:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=LOG_FORMAT
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Process Snapchat export data with improved architecture",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/main_new.py                    # Use default input/output directories
  python src/main_new.py --input custom_input --output custom_output
  python src/main_new.py --log-level DEBUG # Enable debug logging
  python src/main_new.py --no-clean        # Don't clean output directory
        """
    )
    
    parser.add_argument(
        "--input", 
        type=Path, 
        default=INPUT_DIR, 
        help=f"Input directory containing Snapchat export (default: {INPUT_DIR})"
    )
    
    parser.add_argument(
        "--output", 
        type=Path, 
        default=OUTPUT_DIR, 
        help=f"Output directory for processed files (default: {OUTPUT_DIR})"
    )
    
    parser.add_argument(
        "--no-clean", 
        action="store_true", 
        help="Don't clean output directory before processing"
    )
    
    parser.add_argument(
        "--log-level", 
        default="INFO", 
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: INFO)"
    )
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_arguments()
    setup_logging(args.log_level)
    
    # Create and run processor
    processor = Processor(
        input_dir=args.input,
        output_dir=args.output,
        no_clean=args.no_clean
    )
    
    return processor.run()


if __name__ == "__main__":
    sys.exit(main())
