# Bitmoji Integration

## Overview

The Snapchat Media Mapper now automatically fetches and generates Bitmoji avatar images for all users in your export. This provides visual representation of each user that can be used in UI components, contact lists, or conversation displays.

## Features

### 1. **Automatic Avatar Fetching**
- Fetches real Bitmoji avatars from Snapchat's API
- Generates unique fallback avatars for users without Bitmoji
- Fully automatic - no manual intervention required

### 2. **Parallel Processing**
- Uses up to 128 concurrent threads for fast downloads
- Connection pooling and retry logic for reliability
- Efficient session management per thread

### 3. **Smart Fallbacks**
- Color-coded ghost avatars when Bitmoji unavailable
- Deterministic colors (same username = same color always)
- Visual distinction algorithm ensures different users get different colors
- Snapchat-themed ghost icon design

### 4. **Output Format**
- All avatars saved as SVG (scalable, lightweight)
- Stored in `output/bitmoji/` directory
- Filenames based on sanitized usernames
- Collision handling for duplicate names

## How It Works

### Processing Flow

```
1. Generate index.json with all users
     ↓
2. Extract unique usernames
     ↓
3. Parallel fetch Bitmoji avatars (128 workers)
     ↓
4. Save avatars to output/bitmoji/
     ↓
5. Update index.json with avatar paths
```

### API Integration

The tool uses Snapchat's public Snapcode API:
```
GET https://app.snapchat.com/web/deeplink/snapcode
  ?username={username}
  &type=SVG
  &bitmoji=enable
```

Returns an SVG containing the user's Bitmoji avatar.

### Fallback Generation

When Bitmoji is unavailable (user doesn't have one, API fails, etc.):

1. **Hash username** - Use SHA-256 to generate deterministic seed
2. **Select hue** - Map hash to color wheel position (0-360°)
3. **Check separation** - Ensure new color is ≥15° from existing colors
4. **Generate SVG** - Create ghost-themed avatar with unique color

## Output Structure

### Directory Layout
```
output/
└── bitmoji/
    ├── username1.svg          # Real Bitmoji or fallback
    ├── username2.svg
    ├── john-doe.svg
    ├── jane-smith-a3f2c1.svg  # With collision suffix
    └── ...
```

### index.json Format

Each user entry now includes a `bitmoji` field:

```json
{
  "users": [
    {
      "username": "john_doe",
      "display_name": "John Doe",
      "bitmoji": "bitmoji/john-doe.svg"
    },
    {
      "username": "jane_smith",
      "display_name": "Jane Smith",
      "bitmoji": "bitmoji/jane-smith.svg"
    }
  ]
}
```

## Configuration

### Adjusting Worker Count

In `src/bitmoji.py`:

```python
MAX_WORKERS = 128  # Default: 128 concurrent downloads
```

Adjust based on:
- Your network bandwidth
- API rate limits (Snapchat is generally permissive)
- System resources

### Retry Strategy

Default retry configuration:

```python
Retry(
    total=3,              # 3 retry attempts
    backoff_factor=0.4,   # Exponential backoff
    status_forcelist=(429, 500, 502, 503, 504)
)
```

## Performance

### Benchmarks

Typical performance (network dependent):

| User Count | Time (128 workers) | Time (32 workers) |
|------------|-------------------|-------------------|
| 100 users  | ~5-10 seconds     | ~15-20 seconds    |
| 500 users  | ~20-30 seconds    | ~60-90 seconds    |
| 1000 users | ~40-60 seconds    | ~2-3 minutes      |

### Optimization Tips

1. **Network Speed**: Use wired connection for best results
2. **Worker Count**: 128 is optimal for most networks
3. **Retry Logic**: Handles transient failures automatically
4. **Connection Pooling**: Reuses connections across requests

## Error Handling

### Graceful Degradation

The tool never fails due to Bitmoji issues:

```python
try:
    # Fetch real Bitmoji
    avatar = fetch_from_api(username)
except (NetworkError, TimeoutError, APIError):
    # Use fallback avatar
    avatar = generate_fallback(username)
```

### Logging

Bitmoji fetch results are logged:

```
INFO: Starting Bitmoji extraction for 150 users...
WARNING: Could not get Bitmoji for 'user123': HTTP 404. Using fallback.
INFO: Extraction complete: 142 successful, 8 fallbacks (workers=128).
INFO: Saved 150 avatars to 'output/bitmoji/'.
```

## Use Cases

### 1. Web Viewer
Display avatars in conversation lists:
```html
<img src="bitmoji/username.svg" alt="Username" />
```

### 2. Contact List
Build a visual contact list:
```javascript
users.forEach(user => {
  displayContact(user.display_name, user.bitmoji);
});
```

### 3. Message Attribution
Show who sent each message:
```html
<div class="message">
  <img src="${message.sender.bitmoji}" class="avatar" />
  <span>${message.text}</span>
</div>
```

## API Details

### Snapcode API Response

The Snapchat Snapcode API returns an SVG like:

```xml
<svg xmlns="http://www.w3.org/2000/svg" ...>
  <image href="https://cf-st.sc-cdn.net/p/..." />
  <!-- Snapcode QR pattern -->
</svg>
```

We extract the `<image>` element's `href` and create a clean SVG:

```xml
<svg viewBox="0 0 54 54" xmlns="http://www.w3.org/2000/svg">
  <image href="https://cf-st.sc-cdn.net/p/..." x="0" y="0" width="54" height="54"/>
</svg>
```

### Fallback SVG Format

```xml
<svg viewBox="0 0 54 54" xmlns="http://www.w3.org/2000/svg">
  <path d="M27 54.06C33.48..." fill="#3a7c9e" 
        stroke="black" stroke-opacity="0.2" stroke-width="0.9"/>
</svg>
```

The ghost path is the classic Snapchat ghost icon, colored uniquely per user.

## Dependencies

New dependencies added:

```
requests>=2.31.0     # HTTP library
urllib3>=2.0.0       # HTTP client (used by requests)
```

Install with:
```bash
pip install requests urllib3
```

Or use the updated requirements.txt:
```bash
pip install -r requirements.txt
```

## Testing

### Standalone Test

Test the Bitmoji module independently:

```bash
python src/bitmoji.py
```

This will fetch avatars for a sample set of usernames.

### Integration Test

Run the full tool and check output:

```bash
python src/main.py

# Check results
ls output/bitmoji/
cat output/index.json | grep '"bitmoji"'
```

## Troubleshooting

### No Avatars Generated

**Issue**: `output/bitmoji/` is empty

**Solutions**:
1. Check network connectivity
2. Verify `requests` library is installed
3. Check logs for error messages

### Some Users Have No Avatar

**Issue**: Some users missing from `bitmoji/` directory

**Cause**: This is normal! Not all usernames have Bitmoji

**Result**: Fallback avatars are generated automatically

### Slow Download Speed

**Issue**: Bitmoji generation takes too long

**Solutions**:
1. Check network speed
2. Reduce `MAX_WORKERS` if network is saturated
3. Use wired connection instead of WiFi

### API Rate Limiting

**Issue**: Getting 429 Too Many Requests errors

**Solutions**:
1. Reduce `MAX_WORKERS` to 32 or 64
2. Increase `backoff_factor` in retry strategy
3. Run in batches if you have thousands of users

## Future Enhancements

Possible improvements:

- [ ] Cache avatars locally to avoid re-downloading
- [ ] Support other avatar formats (PNG, WebP)
- [ ] Custom fallback designs
- [ ] Avatar size variants (small, medium, large)
- [ ] Progress bar for avatar downloads
- [ ] Batch processing for very large user lists

## Credits

- **Snapcode API**: Snapchat's public API for generating Snapcodes
- **Ghost Icon**: Based on Snapchat's iconic ghost logo
- **Color Algorithm**: Golden ratio color spacing for visual distinction

---

For more information, see:
- [README.md](README.md) - Main documentation
- [src/bitmoji.py](src/bitmoji.py) - Implementation code

