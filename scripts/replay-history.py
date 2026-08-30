#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from atlas_next.replay import render_replay_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("history_database", type=Path)
    parser.add_argument("sample", type=Path)
    parser.add_argument("receipts", type=Path)
    parser.add_argument("--expected-count", type=int, default=15)
    args = parser.parse_args()
    print(
        render_replay_report(
            args.history_database,
            args.sample,
            args.receipts,
            expected_count=args.expected_count,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
