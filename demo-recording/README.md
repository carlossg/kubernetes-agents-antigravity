# AI Canary Analysis Demo Recording

Records a live demo of the AI agent canary analysis system (success + failure
cases) with asciinema, then turns it into a narrated MP4.

## Demo

<video src="ai-canary-demo-narrated.mp4" controls width="100%">
  Your browser (or viewer) doesn't render inline video — download
  <a href="ai-canary-demo-narrated.mp4">ai-canary-demo-narrated.mp4</a> directly.
</video>

Narrated walkthrough: agent architecture, a healthy `green` release getting
unanimously promoted, then a broken `bad-red` release getting debated and
correctly aborted. A silent GIF of the raw recording is also available:
[`ai-canary-demo.gif`](ai-canary-demo.gif).

## Prerequisites

- A healthy cluster with `canary-demo` and the `kubernetes-agent-*` pods
  running (see the main repo's `demo.sh`).
- `kubectl`, the `kubectl-argo-rollouts` plugin, and `jq`.
- `asciinema` for recording.
- `agg` and `ffmpeg` for converting the recording to video
  (`brew install agg ffmpeg`).
- An ElevenLabs API key in `.env` for the voiceover step (see the
  `/demo-video` skill).

## Workflow

1. **Reset the baseline** (not part of the recording):
   ```
   ./prepare-baseline.sh
   ```

2. **Record the demo**:
   ```
   asciinema rec ai-canary-demo.cast
   ./demo-recording.sh
   ```
   Exit the recording (`exit` or Ctrl-D) once `demo-recording.sh` finishes.

3. **Convert to video and get act timestamps**:
   ```
   ./build-video.sh ai-canary-demo.cast
   ```
   This produces `ai-canary-demo.mp4` and prints the wall-clock timestamp
   of each act (they vary run to run since AI response latency isn't
   fixed).

4. **Fill in `talk-track.md`** with the timestamps from step 3, replacing
   each `[[marker]]` placeholder.

5. **Hand off to the `/demo-video` skill**:
   ```
   /demo-video ai-canary-demo.mp4
   ```
   Provide `talk-track.md` as the narration script when asked. You'll
   still need to choose an ElevenLabs voice, a background music track, and
   intro/outro branding — those are genuine preferences, not something
   this pipeline can decide for you.

## Files

| File | Purpose |
|---|---|
| `prepare-baseline.sh` | Resets `canary-demo` to a known-good `blue` baseline before recording |
| `demo-recording.sh` | The actual demo: architecture tour, a healthy `green` release, then a broken `bad-red` release that gets debated and aborted |
| `extract_act_times.py` | Scans a `.cast` file for `ACT_MARKER` lines and reports their timestamps |
| `build-video.sh` | Converts the `.cast` recording to MP4 and runs the timestamp extractor |
| `talk-track.md` | Narration script with placeholder timestamps for the `/demo-video` skill |
