"""OpenRC liveness probe by default; --ready checks live SDR data separately."""
import argparse

from .status import assess, read_status


def healthy(path: str, max_age: float = 15.0, *, ready=False) -> bool:
    try:
        assessment = assess(read_status(path), max_age)
        return assessment['ready' if ready else 'alive']
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError, AttributeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', default='/run/ddt/status.json')
    parser.add_argument('--max-age', type=float, default=15)
    parser.add_argument('--ready', action='store_true')
    args = parser.parse_args()
    return 0 if healthy(args.file, args.max_age, ready=args.ready) else 1


if __name__ == '__main__':
    raise SystemExit(main())
