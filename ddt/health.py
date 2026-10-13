"""OpenRC progress check; uses monotonic time, independent of NTP."""

import argparse
import json
import os
from pathlib import Path
import time


def healthy(path: str, max_age: float = 15.0) -> bool:
    try:
        status = json.loads(Path(path).read_text())
        if type(status["pid"]) is not int or status["pid"] <= 0:
            return False
        age = time.monotonic() - status["updated_monotonic"]
        os.kill(status["pid"], 0)
        return status["state"] != "stopped" and 0 <= age <= max_age
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="/run/ddt/status.json")
    parser.add_argument("--max-age", type=float, default=15)
    args = parser.parse_args()
    return 0 if healthy(args.file, args.max_age) else 1


if __name__ == "__main__":
    raise SystemExit(main())
