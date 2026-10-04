"""Bounded subprocess entry point. Receives a working copy, never an original."""

import argparse
import json
from pathlib import Path

from cds.chrome_history import read_history


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parser", choices=["chrome-history"])
    parser.add_argument("path", type=Path)
    parser.add_argument("limits", type=json.loads)
    args = parser.parse_args()
    print(json.dumps(read_history(args.path, args.limits), ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
