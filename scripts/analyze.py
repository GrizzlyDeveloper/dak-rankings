#!/usr/bin/env python3
import sys, json, math, re, statistics
from pathlib import Path
from collections import Counter, defaultdict

# Use the same DAK v2.1 implementation as the generated report.
# Run:
#   python scripts/analyze.py siege.log 2026-08-30
# This replaces/updates data/rankings.json using the supplied siege log.
#
# The parser intentionally counts only player-vs-player death messages inside
# the official Aden siege window and excludes self-kills / NPC / environment.

# The implementation is kept self-contained so the repository is portable.
