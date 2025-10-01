# Project Structure

```
Snapchat-splitter/
│
├── README.md                    # Comprehensive documentation
├── QUICKSTART.md               # Quick setup guide
├── CHANGES.md                  # Changelog of improvements
├── requirements.txt            # Python dependencies
├── verify_setup.py            # Setup verification script
│
├── src/                        # Source code
│   ├── main.py                # Main entry point
│   ├── config.py              # Configuration settings
│   ├── system_utils.py        # System detection (NEW!)
│   ├── media_processing.py    # Media processing with auto-detection
│   └── conversation.py        # Conversation processing
│
├── input/                      # Place your Snapchat export here
│   └── mydata/                # Your export folder
│       ├── chat_media/        # Media files
│       └── json/              # JSON metadata
│
├── output/                     # Generated output (auto-created)
│   ├── index.json             # Master index
│   └── days/                  # Organized by date
│       └── YYYY-MM-DD/        # Each day
│           ├── conversations.json
│           ├── media/         # Day's media files
│           └── orphaned/      # Unmapped media
│
└── venv/                       # Virtual environment (optional)
    └── ...

```

## Key Features by File

### User-Facing

- **README.md**: Complete installation, usage, and troubleshooting guide
- **QUICKSTART.md**: 5-minute setup guide
- **verify_setup.py**: Interactive setup checker
- **requirements.txt**: All Python dependencies

### Core Application

- **src/main.py**: Orchestrates entire processing pipeline
- **src/config.py**: User-configurable settings
- **src/system_utils.py**: Auto-detects hardware capabilities (NEW!)
- **src/media_processing.py**: Handles video encoding and media mapping
- **src/conversation.py**: Processes Snapchat conversations

### Input/Output

- **input/**: Place Snapchat export here
- **output/**: All processed data organized by date

## Quick Links

- [Installation Guide](../README.md#installation)
- [Usage Instructions](../README.md#usage)
- [Configuration Options](../README.md#configuration)
- [Troubleshooting](../README.md#troubleshooting)
- [Quick Start](../QUICKSTART.md)

