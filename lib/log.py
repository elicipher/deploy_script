"""Line-buffered terminal + private log. Never execute dotenv contents."""
import importlib
import os
from pathlib import Path
import re
import sys

env_file, log_file = map(Path, sys.argv[1:])
fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", buffering=1) as log:
    for line in sys.stdin:
        # Import becomes available after native packages are installed.
        try:
            importlib.invalidate_caches()
            from dotenv import dotenv_values
            values = dotenv_values(env_file, interpolate=False) if env_file.exists() else {}
        except ImportError:
            values = {}
        secrets = {fragment for key, value in values.items()
                   if value and re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", key)
                   for fragment in (value, repr(value)[1:-1], *value.splitlines()) if fragment}
        for value in sorted(secrets, key=len, reverse=True):
            line = line.replace(value, "[REDACTED]")
        line = re.sub(r"(\w+://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", line)
        log.write(line)
        sys.stdout.write(line)
        sys.stdout.flush()
