#!/usr/bin/env python3
"""Builds the narrated AI canary analysis demo video.

Pipeline: PIL slides -> Voicebox TTS per act -> ffmpeg segment assembly
-> concat. No background music (skipped per project decision). Skips
existing artifacts, so re-running only rebuilds what's missing - delete
the relevant file(s) to force a rebuild of one act (see README).
"""
import json
import subprocess
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

BUILD_DIR = Path(__file__).parent
DEMO_DIR = BUILD_DIR.parent
REPO_DIR = DEMO_DIR.parent

SOURCE_VIDEO = DEMO_DIR / "ai-canary-demo.mp4"
LOGO_PATH = Path("/Users/sanchezg/dev/carlossg/argo-rollouts/rollouts-demo/logo.png")
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"


def _probe_resolution(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(path)],
        check=True, capture_output=True, text=True,
    )
    w, h = out.stdout.strip().split("x")
    return int(w), int(h)


# Slides are sized to match the source recording so concatenation doesn't
# need to rescale - the recording's terminal cols/rows control this, not a
# fixed constant, since re-recording at different dimensions is expected.
WIDTH, HEIGHT = _probe_resolution(SOURCE_VIDEO)
BG_COLOR = (11, 26, 41)  # dark navy, complements the mascot's orange/blue
FG_COLOR = (255, 255, 255)

# Voicebox runs as a local app (Voicebox.app); the MCP tool and this script
# both talk to the same local HTTP server.
VOICEBOX_URL = "http://localhost:17493"
VOICEBOX_PROFILE_ID = "7d76758c-cf45-40ff-9170-df776d95c51b"  # Adam (preset)
VOICEBOX_ENGINE = "kokoro"  # the only engine the Adam preset profile supports

# (id, kind, start, end, narration) - kind is "slide" or "video".
# Timestamps are seconds into ai-canary-demo.mp4 (see talk-track.md).
ACTS = [
    ("00-intro", "slide", None, None,
     "With Argo Rollouts, a canary release doesn't have to be judged by a "
     "single static threshold. Here, three AI agents actually read the "
     "logs, metrics, and events, debate what they find, and vote on "
     "whether to promote or abort."),
    ("01-act1", "video", 0.0, 8.62,
     "Meet the team: one orchestrator, three specialists, logs, metrics, "
     "and events, plus a log proxy that safely gives the sandboxed log "
     "agent real pod data without breaking the cluster's security policy. "
     "Every canary release gets debated by all three before a verdict is "
     "reached."),
    ("02-act2", "video", 8.62, 150.06,
     "Let's ship a healthy release. We update the canary to the green "
     "image and watch Argo Rollouts step through the canary weights "
     "while the agents analyze it in the background."),
    ("03-act3", "video", 150.06, 157.46,
     "The verdict: full consensus to promote. The agents fetched real "
     "pod logs, checked resource metrics, and confirmed a clean event "
     "history. Every agent agrees this release is safe."),
    ("04-act4", "video", 157.46, 162.36,
     "And there it is, green is now the new stable, promoted "
     "automatically, no human in the loop."),
    ("05-act5", "video", 162.36, 165.16,
     "Now the interesting part. We deploy a broken build, bad-red, which "
     "has a deliberate regression baked in."),
    ("06-act6", "video", 165.16, 242.23,
     "While Argo Rollouts holds the canary at partial traffic, the "
     "orchestrator fans out to the three specialists, gathers their "
     "reports, and runs a live debate to reach consensus, in real time, "
     "on this actual cluster."),
    ("07-act7", "video", 242.23, 268.28,
     "There's the split: metrics and events looked clean, but the log "
     "analyst actually read the pods' logs and caught the real "
     "regression. The orchestrator sides with the hard evidence, not "
     "just clean-looking metrics."),
    ("08-act8", "video", 268.28, 282.28,
     "Argo Rollouts aborts automatically. Blue stays stable, the broken "
     "release never reaches real users, and the whole decision was made "
     "by AI agents debating real telemetry, not a static threshold."),
    ("09-outro", "slide", None, None,
     "Real logs. Real debate. Real decisions. That's AI-powered canary "
     "analysis for Argo Rollouts."),
]


def run(cmd):
    subprocess.run(cmd, check=True)


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


# ---------------------------------------------------------------------------
# Font sizes/offsets below were tuned against a 790x560 reference canvas;
# scale them to whatever resolution the actual recording produced.
_SCALE = HEIGHT / 560


def make_intro_slide():
    path = BUILD_DIR / "images" / "00-intro.png"
    if path.exists():
        return
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype(FONT_PATH, round(40 * _SCALE))
    subtitle_font = ImageFont.truetype(FONT_PATH, round(22 * _SCALE))

    title = "AI-Powered Canary Analysis"
    subtitle = "Argo Rollouts + Gemini Agents"

    tb = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((WIDTH - (tb[2] - tb[0])) / 2, HEIGHT / 2 - 50 * _SCALE), title,
               font=title_font, fill=FG_COLOR)
    sb = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    draw.text(((WIDTH - (sb[2] - sb[0])) / 2, HEIGHT / 2 + 10 * _SCALE), subtitle,
               font=subtitle_font, fill=(180, 195, 210))
    img.save(path)
    print(f"wrote {path}")


def make_outro_slide():
    path = BUILD_DIR / "images" / "09-outro.png"
    if path.exists():
        return
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo_size = round(280 * _SCALE)
    logo.thumbnail((logo_size, logo_size))
    img.paste(logo, ((WIDTH - logo.width) // 2, (HEIGHT - logo.height) // 2 - round(20 * _SCALE)), logo)

    draw = ImageDraw.Draw(img)
    tag_font = ImageFont.truetype(FONT_PATH, round(20 * _SCALE))
    tagline = "Real logs. Real debate. Real decisions."
    tb = draw.textbbox((0, 0), tagline, font=tag_font)
    draw.text(((WIDTH - (tb[2] - tb[0])) / 2, HEIGHT - 70 * _SCALE), tagline,
               font=tag_font, fill=FG_COLOR)
    img.save(path)
    print(f"wrote {path}")


# ---------------------------------------------------------------------------
def make_tts(act_id, text, poll_timeout=180):
    path = BUILD_DIR / "audio" / f"{act_id}.wav"
    if path.exists():
        return path

    resp = requests.post(
        f"{VOICEBOX_URL}/generate",
        json={"profile_id": VOICEBOX_PROFILE_ID, "text": text, "engine": VOICEBOX_ENGINE},
        timeout=30,
    )
    resp.raise_for_status()
    generation_id = resp.json()["id"]

    # /status is SSE: a single GET blocks and streams "data: {...}" events
    # (e.g. "generating" then "completed"/"error") until the job reaches a
    # terminal state, then closes. Take the last event as the final status.
    deadline = time.monotonic() + poll_timeout
    status = None
    while time.monotonic() < deadline:
        status_resp = requests.get(
            f"{VOICEBOX_URL}/generate/{generation_id}/status", timeout=poll_timeout
        )
        status_resp.raise_for_status()
        events = [
            json.loads(line[len("data:"):].strip())
            for line in status_resp.text.strip().splitlines()
            if line.startswith("data:")
        ]
        if events:
            status = events[-1]
            if status["status"] == "completed":
                break
            if status["status"] == "error":
                raise RuntimeError(f"TTS failed for {act_id}: {status.get('error')}")
        time.sleep(1)
    else:
        raise RuntimeError(f"TTS timed out for {act_id} after {poll_timeout}s (last status: {status})")

    audio_resp = requests.get(f"{VOICEBOX_URL}/audio/{generation_id}", timeout=60)
    audio_resp.raise_for_status()
    path.write_bytes(audio_resp.content)
    print(f"wrote {path} ({len(audio_resp.content)} bytes)")
    return path


# ---------------------------------------------------------------------------
ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "25",
    "-c:a", "aac", "-ac", "2", "-b:a", "192k",
]


def make_slide_segment(act_id, image_path, audio_path):
    seg_path = BUILD_DIR / "segments" / f"{act_id}.mp4"
    if seg_path.exists():
        return
    vo_duration = ffprobe_duration(audio_path)
    duration = vo_duration + 0.5
    run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-filter_complex",
        f"[1:a]aresample=async=1,apad=pad_dur=0.5,pan=stereo|c0=c0|c1=c0[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-t", str(duration),
        *ENCODE_ARGS,
        str(seg_path),
    ])
    print(f"wrote {seg_path}")


# When a video clip runs much longer than its narration (e.g. a live agent
# debate that took over a minute against the real cluster), we time-lapse it
# down to the narration's length instead of leaving the tail playing in
# silence. Above this speed factor we burn in a "Sped up Nx" label so it
# reads as an intentional time-lapse rather than broken video.
SPEED_LABEL_THRESHOLD = 2.5


def make_video_segment(act_id, start, end, audio_path):
    seg_path = BUILD_DIR / "segments" / f"{act_id}.mp4"
    if seg_path.exists():
        return
    clip_duration = end - start
    vo_duration = ffprobe_duration(audio_path)
    target_duration = vo_duration + 0.3

    if clip_duration <= target_duration:
        pad_time = target_duration - clip_duration
        video_filter = f"tpad=stop_mode=clone:stop_duration={pad_time}" if pad_time > 0 else "null"
        speed_factor = 1.0
    else:
        speed_factor = clip_duration / target_duration
        video_filter = f"setpts=PTS/{speed_factor:.6f}"
        if speed_factor >= SPEED_LABEL_THRESHOLD:
            label = f"Sped up {speed_factor:.0f}x" if speed_factor >= 10 else f"Sped up {speed_factor:.1f}x"
            video_filter += (
                f",drawtext=fontfile='{FONT_PATH}':text='{label}':fontsize=28:"
                "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:x=w-tw-30:y=30"
            )
    total_duration = target_duration

    audio_filter = "[1:a]adelay=300|300,aresample=async=1,apad=pad_dur=1,pan=stereo|c0=c0|c1=c0[aout]"

    run([
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end), "-i", str(SOURCE_VIDEO),
        "-i", str(audio_path),
        "-filter_complex", f"[0:v]{video_filter}[vout];{audio_filter}",
        "-map", "[vout]", "-map", "[aout]",
        "-t", str(total_duration),
        *ENCODE_ARGS,
        str(seg_path),
    ])
    print(f"wrote {seg_path} (clip={clip_duration:.1f}s vo={vo_duration:.1f}s speed={speed_factor:.2f}x)")


# ---------------------------------------------------------------------------
def concatenate():
    final_path = DEMO_DIR / "ai-canary-demo-narrated.mp4"
    if final_path.exists():
        print(f"{final_path} already exists, skipping concat")
        return final_path

    concat_txt = BUILD_DIR / "concat.txt"
    seg_dir = BUILD_DIR / "segments"
    segments = sorted(seg_dir.glob("*.mp4"))
    with open(concat_txt, "w") as f:
        for seg in segments:
            f.write(f"file '{seg.resolve()}'\n")

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt),
        *ENCODE_ARGS,
        str(final_path),
    ])
    print(f"wrote {final_path}")
    return final_path


# ---------------------------------------------------------------------------
def main():
    make_intro_slide()
    make_outro_slide()

    for act_id, kind, start, end, text in ACTS:
        audio_path = make_tts(act_id, text)
        if kind == "slide":
            image_path = BUILD_DIR / "images" / f"{act_id}.png"
            make_slide_segment(act_id, image_path, audio_path)
        else:
            make_video_segment(act_id, start, end, audio_path)

    final = concatenate()
    print(f"\nDone: {final}")


if __name__ == "__main__":
    main()
