# Pre-Next-Call Lifecycle Revision

This file is the implementation specification for one revision only. It does
not replace `PROMPT-CACHE-LIFECYCLE.md`. The revision preserves the intentional
CAAL-owned `mlx-lm` model server and changes where transition, summary, tool,
and prompt-cache work is owned.

## Objectives

1. Delete an ended LiveKit room promptly; room occupancy must not represent
   maintenance completion.
2. Keep the outgoing job responsible only for stopping allocation, persisting
   completed turns, and bounded non-inference cleanup.
3. Make the replacement job complete recovery and summarize the selected
   conversation before its greeting begins.
4. Allow only session-prefix cache loading to overlap the canned greeting.
5. Discover model-visible tool schemas at service startup or explicit toolset
   invalidation, never during ordinary call preparation.
6. Preserve one inference at a time inside a voice job and cap model-server
   concurrency at four.
7. Keep exactly two memory-transition behaviors: graceful teardown and hard
   reset. Failed graceful recovery escalates to the hard-reset behavior.

## Required Ordering

### Service startup

Before the service declares itself ready:

1. Load the selected model, runtime, kernels, STT, and TTS.
2. Discover the canonical model-visible tool schemas.
3. Sort and deduplicate the schemas deterministically.
4. Serialize them canonically and calculate a toolset fingerprint.
5. Persist a versioned stable prompt bundle containing:
   - stable system instructions;
   - exact tool schemas;
   - toolset fingerprint;
   - language and model identity needed to validate reuse.
6. Warm the stable prefix from that exact bundle.

Tool execution clients are not part of the stable bundle. Each job may bind
MCP, n8n, Home Assistant, memory, and web-search callables, but every bound
model-visible name and schema must match the stable bundle before the call can
accept user turns.

### Explicit tool reload

A tool reload is a stable-prefix invalidation, not an in-call schema mutation:

1. Discover and canonicalize the new schemas.
2. Replace the versioned stable bundle atomically.
3. Warm the new stable prefix.
4. Future calls use the new fingerprint.
5. An active call retains its frozen tool generation until it ends.

### Model change

Selecting a different model is a stable-prefix invalidation, and it is only
permitted while idle:

1. Refuse the change whenever a call is active. A live call has frozen its
   session prefix and tool generation against the model it started on, and the
   model server holds one model at a time, so a mid-call switch could only be
   ignored or destructive. The refusal is a 409 and leaves settings untouched.
2. Persist the selection. It is the single source of truth for which model
   CAAL uses; the launcher starts the server on that same value so the server
   default cannot become a competing identity.
3. Rebuild the versioned stable prompt bundle for the new model identity. Tool
   schemas are unchanged, but the bundle records the model it was built for.
4. Load the new model and warm its stable prefix before reporting success.
5. The next call therefore finds a valid bundle and a warm model, and performs
   no discovery or warm-up of its own.

Settings unrelated to the model remain writable during a call. If warming
fails, the selection still stands: the next call rebuilds, which is slow rather
than broken.

### Outgoing session

When a user hangs up or switches conversations:

1. Refuse new turns and cancel or finish job-local inference.
2. Persist completed transcript messages; interrupted assistant output remains
   display-only and is excluded from future context and summaries.
3. Close the AgentSession.
4. Run only bounded, non-inference shutdown-callback cleanup, including an
   immediate idempotent clear of call-scoped model and speech caches.
5. Do not summarize and do not warm a model prefix in the shutdown callback.
   One bounded single-token generation is permitted, and only to release
   call-scoped KV: mlx-lm holds it in a batch generator that no endpoint can
   reach, and only another generation reallocates it. Nothing reads the reply
   and no durable state is touched. Cycling the generation thread would also
   release it but orphans the retained weights' Metal streams; unloading the
   model would break the model/runtime foundation rule.

LiveKit disconnects the room before awaiting shutdown callbacks. Consequently,
the frontend must not poll participant count as a proxy for callback progress.
It deletes the fixed room promptly and starts the replacement flow.

### Pre-call preparation for every selected call

Before every selected call plays its greeting, including the first call after
service startup or restart:

1. Acquire the transition/inference barrier for the CAAL-owned model service.
2. Ensure the preceding request cannot allocate further memory.
3. For a pending graceful memory trip, clear call caches and check recovery for
   no more than five seconds. Escalate immediately if recovery fails.
4. For critical macOS pressure, perform the hard reset before ordinary
   preparation and verify model readiness. Expected recovery is 10-20 seconds.
5. Load the selected conversation's durable summary checkpoint.
6. If eligible unsummarized messages remain, perform one bounded summary
   checkpoint before constructing the session. Rolling summaries should make
   this uncommon. Summary failure keeps the previous checkpoint.
7. On summary failure, include a durable unsummarized fallback tail up to the
   normal 6,000-character summary-input budget so continuity does not depend on
   the smaller successful-checkpoint replay window.
8. Load current user memory and session date/time.
9. Read the existing stable prompt bundle; do not rediscover tool schemas.
10. Bind execution handlers and verify that their model-visible schemas match
    the bundle fingerprint.
11. Freeze the complete session context and tool generation.

No greeting may begin before these steps finish. Recovery, model loading,
summary generation, tool discovery, and tool binding must not be hidden behind
the greeting.

### Greeting interval

Only one operation may overlap the canned greeting: model prefill of the exact
frozen session prefix:

```text
stable system instructions
+ exact stable tool definitions
+ session date/time
+ current user memory
+ finalized conversation summary
+ bounded unsummarized tail
```

The warm-up task starts immediately before the canned greeting. Greeting
interruption does not cancel it. A first user turn waits for that same task, so
it cannot create a second inference. Session-cache state is published outside
model-visible history as `loading`, followed by `ready` or `failed`.

## Memory Transition Rules

### Graceful

Manual endings, switches, and the MLX allocation boundary use graceful
behavior:

1. Gate new turns.
2. End the old request/session.
3. Clear prompt and speech caches through serialized model-service controls.
4. Check recovery for at most five seconds.
5. Continue when recovered; otherwise escalate to hard reset.

### Hard reset

Critical macOS pressure enters hard reset directly. Failed graceful recovery
uses the same path:

1. Gate new turns.
2. Interrupt the active model workload.
3. Restart or fully reset the supervised model server rather than merely
   dropping Python references while a generation thread may still be running.
4. Verify the API and selected model are ready.
5. Continue with pre-next-call summary and session construction.

The memory guard must consume MLX allocation counters from `/v1/memory` when
available. Process footprint remains the fallback/model-local corroborating
signal. The integrated macOS pressure mapping is:

| Signal | Behavior |
| --- | --- |
| MLX allocation reaches configured cap | Graceful trip |
| macOS urgent pressure | Observe and log only; no trip |
| macOS critical pressure | Hard reset first |
| Model footprint cap when MLX metrics are unavailable | Graceful trip |

## Model-Server Administration

The CAAL-owned `mlx-lm` direction is retained. Administrative operations must
not mutate `ResponseGenerator`, `ModelProvider`, prompt-cache, or generation
thread internals concurrently with inference.

Required controls:

- cache clear is serialized after the active request stops;
- hard reset interrupts generation and resets the whole supervised model
  process, request queue, MLX allocator, and caches;
- readiness is explicit and bounded;
- private `mlx-lm==0.31.3` integration points have startup compatibility
  assertions and integration coverage;
- model-server prompt and decode concurrency both default to four;
- CAAL voice inference remains serialized to one per job.

## Conversation Summary Rules

- Persist messages continuously.
- Trigger rolling summary only after 4,000 unsummarized characters at completed
  assistant-turn boundaries.
- Rolling summary shares the voice inference lock and yields to a live turn.
- Pre-next-call summary is independent of the rolling threshold and processes
  at most 6,000 unsummarized characters per request.
- Summary updates atomically compare and advance `summary_through_rowid`.
- Failure retains the previous summary and checkpoint and must not trigger a
  second reset.
- A conversation switched away from need not be summarized until it is next
  selected; its full transcript remains durable.

## Acceptance Criteria

1. The frontend reset route contains no 35-second participant drain.
2. The outgoing shutdown callback performs no semantic LLM request. The single-token cache drain described above is the only exception, and it must remain bounded and reply-agnostic.
3. An ordinary hangup clears call-scoped model and speech caches even when no
   replacement call starts; pre-call cleanup repeats this only as a fallback.
4. Summary completion precedes greeting start whenever the selected
   conversation has an eligible unsummarized delta, including the first call
   after service startup or restart.
5. Ordinary calls consume a versioned stable tool bundle without discovering
   model-visible tool schemas.
6. Tool reload replaces and warms the stable bundle for future calls without
   changing the active call's generation.
7. Only session-prefix prefill overlaps the greeting.
8. Greeting interruption cannot create concurrent warm-up and live inference.
9. MLX allocation can trigger graceful session ending; critical pressure uses
   hard reset first. Urgent pressure alone is observational.
10. Model administration cannot race active generation.
11. Server concurrency defaults to four.
12. Python tests, Ruff, frontend lint/build, shell syntax, and diff checks pass.
13. Normal conversation switching has no fixed maintenance sleep; hard-reset
    readiness remains bounded to the expected 10-20-second range.
