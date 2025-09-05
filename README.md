# Snapchat Media Mapper

Process an unzipped Snapchat data export to intelligently organize your chat history and media. The script merges media files (like videos) with their corresponding filter overlays, maps each media file back to its specific message in a conversation, and organizes the results into a clean, browsable structure.

## Key Features

- **Overlay Merging:** Automatically merges video files with their corresponding overlay files (which contain filters, timestamps, etc.) using FFmpeg.
- **Accurate Media Mapping:** Links media files to the exact messages they belong to—first by using the media ID and then by falling back to a timestamp comparison.
- **Conversation‑Based Organization:** Sorts all chats and media into individual folders for each conversation and group chat.
- **Detailed History:** Generates a `conversation.json` for each chat, containing a merged history of messages and snaps, along with conversation metadata.
- **Orphaned Media Handling:** Any media that cannot be mapped to a specific conversation is moved to an `orphaned` folder for manual review.
- **Handles Complex Cases:** Correctly processes multi‑part videos and grouped snaps with multiple overlays.
- **Detailed Summary:** Provides a comprehensive summary upon completion, detailing how many files were processed, mapped, and merged.

## Requirements

- **Python 3.7+**
- **FFmpeg:** Must be installed and available on your system `PATH` (required to merge videos with their overlays). You can download it from the official FFmpeg website.
- This project uses **only Python’s standard libraries**—no additional `pip` packages required.

## How to Use

1. **Install FFmpeg**  
   If you haven’t already, download FFmpeg from the official website and make sure the executable is on your system `PATH`. This is a one‑time setup.

2. **Add Your Data**  
   Place your unzipped Snapchat export folder (e.g., `mydata_...`) into the `input` directory.

   Your directory structure should look like:

   ```text
   ./
   ├── input/
   │   └── mydata/             <-- Your unzipped Snapchat export folder
   │       ├── json/
   │       └── chat_media/
   └── src/
       ├── main.py
       ├── config.py
       └── ...
   ```

3. **Run the Script**  
   From this project’s root directory, run:

   ```bash
   python src/main.py
   ```

4. **Check the Output**  
   A new `output` directory will be created. Inside, you’ll find organized conversations, groups, and any orphaned media.

   ```text
   ./
   └── output/
       ├── conversations/
       │   └── 2023-11-01 - John Doe/
       │       ├── media/
       │       └── conversation.json
       ├── groups/
       │   └── 2023-10-30 - Friends Group/
       │       ├── media/
       │       └── conversation.json
       └── orphaned/
           └── ...
   ```

## File Descriptions

- **`main.py`** — The main entry point that orchestrates the entire process from initialization to cleanup.
- **`config.py`** — Contains configuration settings (like directory paths) and helper utility functions.
- **`conversation.py`** — Handles processing and merging of chat/snap history JSON files and generates conversation metadata.
- **`media_processing.py`** — Contains all logic for media handling, including merging overlays with FFmpeg, indexing files, and mapping media to messages.
