"""Bounded subprocess entry point. Receives a working copy, never an original."""

import argparse
import json
from pathlib import Path

import cds.chrome_history as chrome_history
import cds.firefox_history as firefox_history
import cds.history_records as history_records


def read_history(path, limits, parser="browser-history"):
    readers = [chrome_history.read_visits, firefox_history.read_visits]
    if parser == "chrome-history":
        readers = [chrome_history.read_visits]
    elif parser == "firefox-history":
        readers = [firefox_history.read_visits]
    return history_records.read_history(path, limits, readers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parser", choices=["browser-history", "chrome-history", "firefox-history"])
    parser.add_argument("path", type=Path)
    parser.add_argument("limits", type=json.loads)
    args = parser.parse_args()
    print(json.dumps(read_history(args.path, args.limits, args.parser), ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
