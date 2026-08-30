#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from atlas_next.history import CAPABILITY_FAMILY, render_report


parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
args = parser.parse_args()
print(render_report(args.database, set(CAPABILITY_FAMILY)))
