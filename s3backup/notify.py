"""Best-effort macOS desktop notifications.

Uses ``osascript`` so there's no dependency. Notifications are advisory — if
they fail (non-macOS, no GUI session, sandboxed launchd context), the run
result is always still available via ``s3backup status``, so we never raise.
"""

import shutil
import subprocess


def notify(title: str, message: str) -> bool:
    """Show a desktop notification. Returns True if the command ran cleanly."""
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    # Escape double quotes for the AppleScript string literals.
    safe_msg = message.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    try:
        result = subprocess.run(
            [osascript, "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
