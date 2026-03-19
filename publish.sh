#!/bin/bash
# Wrapper called by SwiftBar — logs all output to /tmp/obsidian-publisher.log
PYTHON="/Users/jalen/Projects/jhlj.studio/.venv/bin/python3"
SCRIPT="/Users/jalen/Projects/jhlj.studio/obsidian_publisher.py"
LOG="/tmp/obsidian-publisher.log"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
"$PYTHON" "$SCRIPT" >> "$LOG" 2>&1
