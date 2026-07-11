#!/usr/bin/env bash
# Turns the asciinema recording into an MP4 and reports the exact timestamp
# of each act, ready to paste into talk-track.md before handing off to the
# /demo-video skill for voiceover + slides + music assembly.
#
# Usage: ./build-video.sh path/to/recording.cast
#
# Requires:
#   agg     - asciinema -> gif renderer (brew install agg)
#   ffmpeg  - gif -> mp4 (brew install ffmpeg)
#   python3 - for timestamp extraction

set -euo pipefail

CAST_FILE="${1:?usage: build-video.sh path/to/recording.cast}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$(dirname "${CAST_FILE}")"
BASE_NAME="$(basename "${CAST_FILE}" .cast)"
GIF_FILE="${OUT_DIR}/${BASE_NAME}.gif"
MP4_FILE="${OUT_DIR}/${BASE_NAME}.mp4"

for tool in agg ffmpeg python3; do
	if ! command -v "${tool}" &>/dev/null; then
		echo "❌ Missing dependency: ${tool}" >&2
		echo "   brew install agg ffmpeg" >&2
		exit 1
	fi
done

echo "==> Rendering ${CAST_FILE} to GIF..."
# --idle-time-limit disabled (agg defaults to compressing pauses over 5s):
# our pauses (canary step waits, the log-tail act) are deliberate and must
# keep their real duration so extract_act_times.py's timestamps - taken
# from the raw .cast - stay valid for this video's timeline.
agg --idle-time-limit 999999 "${CAST_FILE}" "${GIF_FILE}"

echo "==> Converting GIF to MP4..."
ffmpeg -y -i "${GIF_FILE}" \
	-movflags faststart \
	-pix_fmt yuv420p \
	-vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
	"${MP4_FILE}"

echo ""
echo "==> Act timestamps (source recording, seconds from start):"
python3 "${SCRIPT_DIR}/extract_act_times.py" "${CAST_FILE}"

echo ""
echo "==> Done."
echo "    Video:   ${MP4_FILE}"
echo "    Next:    fill the timestamps above into talk-track.md, then run"
echo "             the /demo-video skill:"
echo ""
echo "               /demo-video ${MP4_FILE}"
echo ""
echo "             and provide talk-track.md as the narration script."
echo "             You'll still need to pick: an ElevenLabs voice, a"
echo "             background music track, and intro/outro branding -"
echo "             those are your call, not something this script can guess."
