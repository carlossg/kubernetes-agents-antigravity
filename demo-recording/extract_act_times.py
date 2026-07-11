#!/usr/bin/env python3
"""Extract wall-clock timestamps of ACT_MARKER lines from an asciinema .cast file.

AI agent response latency varies run to run, so act boundaries can't be
hardcoded into the talk track ahead of time. This scans the recording for
the marker lines demo-recording.sh prints before each act and reports the
timestamp (in seconds from recording start) at which each one appeared.

Usage:
    python3 extract_act_times.py path/to/recording.cast
"""
import json
import re
import sys

MARKER_RE = re.compile(r"ACT_MARKER:\s*(\S+)\s*###")


def extract(cast_path: str) -> list[tuple[str, float]]:
    found: dict[str, float] = {}
    buffer = ""
    with open(cast_path) as f:
        header = f.readline()  # v2 format: first line is a JSON header, skip it
        json.loads(header)
        for line in f:
            line = line.strip()
            if not line:
                continue
            time, event_type, data = json.loads(line)
            if event_type != "o":
                continue
            buffer = (buffer + data)[-500:]
            for match in MARKER_RE.finditer(buffer):
                marker_id = match.group(1)
                if marker_id not in found:
                    found[marker_id] = time
    return sorted(found.items(), key=lambda kv: kv[1])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: extract_act_times.py <recording.cast>", file=sys.stderr)
        sys.exit(1)

    acts = extract(sys.argv[1])
    if not acts:
        print("No ACT_MARKER lines found - did demo-recording.sh run inside this recording?", file=sys.stderr)
        sys.exit(1)

    width = max(len(name) for name, _ in acts)
    for name, t in acts:
        print(f"{name:<{width}}  {t:8.2f}s")
