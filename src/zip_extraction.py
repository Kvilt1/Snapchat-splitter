"""Zip extraction with timestamp preservation from local file headers."""

import logging
import os
import re
import struct
import sys
import time
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def get_local_mtime(zf_file, info):
    """Read the LOCAL file header's extended timestamp (0x5455) for accurate UTC mtime.

    Args:
        zf_file: Open file handle for the zip file
        info: ZipInfo object for the entry

    Returns:
        UTC mtime as a Unix timestamp (float)
    """
    try:
        zf_file.seek(info.header_offset + 26)
        fname_len, extra_len = struct.unpack("<HH", zf_file.read(4))
        zf_file.seek(fname_len, 1)
        extra = zf_file.read(extra_len)

        i = 0
        while i + 4 <= len(extra):
            tag, size = struct.unpack_from("<HH", extra, i)
            i += 4
            if tag == 0x5455 and size >= 5:
                flags = extra[i]
                if flags & 1:
                    return struct.unpack_from("<I", extra, i + 1)[0]
            i += size
    except Exception:
        pass
    return time.mktime(info.date_time + (0, 0, -1))


def extract_zips(input_dir: Path, tmp_dir: Path) -> None:
    """Extract only json/ and chat_media/ from zip files, preserving timestamps.

    Handles primary zips and secondary numbered zips (e.g., export-1.zip, export-2.zip).
    Preserves file modification times from the zip's local file headers for accurate
    timestamp-based media mapping.

    Args:
        input_dir: Directory containing zip files
        tmp_dir: Temporary directory to extract into

    Raises:
        SystemExit: If no zip files found or required directories not in zips
    """
    zips = sorted(input_dir.glob("*.zip"))
    if not zips:
        return  # No zips to extract, caller will look for folder structure

    primary = [z for z in zips if not re.search(r"-\d+\.zip$", z.name)]
    secondary = sorted(
        [z for z in zips if re.search(r"-\d+\.zip$", z.name)],
        key=lambda z: int(re.search(r"-(\d+)\.zip$", z.name).group(1))
    )

    all_zips = primary + secondary
    logger.info(f"Found {len(all_zips)} zip file(s) to extract")

    for idx, zf_path in enumerate(all_zips, 1):
        logger.info(f"[{idx}/{len(all_zips)}] Extracting: {zf_path.name}")

        with zipfile.ZipFile(zf_path) as zf:
            relevant = [
                i for i in zf.infolist()
                if any(p in Path(i.filename).parts for p in ("json", "chat_media"))
            ]

            if not relevant:
                continue

            raw = open(zf_path, "rb")
            for i, info in enumerate(relevant, 1):
                parts = Path(info.filename).parts
                for j, part in enumerate(parts):
                    if part in ("json", "chat_media"):
                        rel = Path(*parts[j:])
                        dest = tmp_dir / rel

                        if info.is_dir():
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(zf.read(info.filename))
                            mtime = get_local_mtime(raw, info)
                            os.utime(dest, (mtime, mtime))
                        break
            raw.close()

    if not (tmp_dir / "json").exists() or not (tmp_dir / "chat_media").exists():
        raise FileNotFoundError(
            "json/ or chat_media/ not found in zip files. "
            "Ensure your Snapchat export zips contain these directories."
        )

    logger.info(f"Zip extraction complete -> {tmp_dir}")
