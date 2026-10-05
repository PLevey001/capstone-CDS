"""Bounded subprocess entry point. Receives a working copy, never an original."""

import argparse
import json
from pathlib import Path

import cds.chrome_history as chrome_history
import cds.firefox_history as firefox_history
import cds.history_records as history_records
import cds.registry_records as registry_records


def read_history(path, limits, parser="browser-history", *, journal_aware=False):
    readers = [chrome_history.read_visits, firefox_history.read_visits]
    if parser == "chrome-history":
        readers = [chrome_history.read_visits]
    elif parser == "firefox-history":
        readers = [firefox_history.read_visits]
    return history_records.read_history(path, limits, readers, journal_aware=journal_aware)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parser", choices=["browser-history", "chrome-history", "firefox-history", "windows-registry"])
    parser.add_argument("path", type=Path)
    parser.add_argument("limits", type=json.loads)
    parser.add_argument("--journal-aware", action="store_true", help="Only for disposable in-image working sets")
    args = parser.parse_args()
    result = (registry_records.read_hive(args.path, args.limits) if args.parser == "windows-registry"
              else read_history(args.path, args.limits, args.parser, journal_aware=args.journal_aware))
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
