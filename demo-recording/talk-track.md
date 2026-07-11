# AI Canary Analysis — Talk Track

Timestamps below are from `ai-canary-demo.mp4` (re-recorded 2026-07-11 at
220x50 to fix line-wrapping in the original 80-col capture; 282s total;
extracted via `build-video.sh ai-canary-demo.cast`). If the demo is
re-recorded again, re-run `build-video.sh` and update these — AI response
latency varies run to run, so exact act boundaries shift.

## Intro (slide, not from the recording)

With Argo Rollouts, a canary release doesn't have to be judged by a single
static threshold. Here, three AI agents actually read the logs, metrics,
and events — debate what they find — and vote on whether to promote or
abort.

## Act 1 — Meet the agents (0:00–0:08)

Meet the team: one orchestrator, three specialists — logs, metrics, and
events — plus a log proxy that safely gives the sandboxed log agent real
pod data without breaking the cluster's security policy. Every canary
release gets debated by all three before a verdict is reached.

## Act 2 — A healthy release (0:08–2:30)

Let's ship a healthy release. We update the canary to the green image and
watch Argo Rollouts step through the canary weights while the agents
analyze it in the background.

## Act 3 — The verdict is unanimous (2:30–2:37)

The verdict: full consensus to promote. The agents fetched real pod logs,
checked resource metrics, and confirmed a clean event history — every
agent agrees this release is safe.

## Act 4 — Promoted (2:37–2:42)

And there it is — green is now the new stable, promoted automatically,
no human in the loop.

## Act 5 — Now the interesting part (2:42–2:45)

Now the interesting part. We deploy a broken build — bad-red — which has
a deliberate regression baked in.

## Act 6 — Watching the agents debate (2:45–4:02)

While Argo Rollouts holds the canary at partial traffic, the orchestrator
fans out to the three specialists, gathers their reports, and runs a live
debate to reach consensus — in real time, on this actual cluster.

## Act 7 — A split decision (4:02–4:28)

There's the split: metrics and events looked clean — but the log analyst
actually read the pods' logs and caught the real regression. The
orchestrator sides with the hard evidence, not just clean-looking metrics.

## Act 8 — Aborted, automatically (4:28–4:45)

Argo Rollouts aborts automatically. Blue stays stable, the broken release
never reaches real users — and the whole decision was made by AI agents
debating real telemetry, not a static threshold.

## Outro (slide, not from the recording)

Real logs. Real debate. Real decisions - that's AI-powered canary analysis
for Argo Rollouts.
