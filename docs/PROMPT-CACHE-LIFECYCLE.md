# CAAL Memory, Prompt, and Conversation Lifecycle

This document defines the steady-state lifecycle. The implementation checklist
for the pre-next-call revision is in `PRE-NEXT-CALL-REVISION.md`.

CAAL has six user-visible stages implemented by four shared operations:

```text
initialize service foundation                 Stage 1
prepare and warm a frozen call                Stages 2 and 4
process a live turn                           Stages 3 and 5
stop allocation and hand off durable state    Stage 6
```

Stages 2 and 4 use one preparation path. Stages 3 and 5 use one turn path.

## Lifecycle

| Stage | General principle | Memory occupants |
| --- | --- | --- |
| **1. Service startup -> ready for call** | Load shared model, speech, and runtime components. Discover canonical model-visible tool schemas once, publish a versioned stable prompt bundle, and warm that exact stable prefix before declaring readiness. | Model parameters; `mlx-lm` runtime and kernels; allocator pools; STT/TTS parameters; stable system instructions and exact tool definitions. |
| **2. First call starts -> before greeting ends** | Before greeting, including the first call after service startup or restart, complete transition recovery and summarize the selected conversation whenever it has an eligible unsummarized delta. Then load user memory/date/history, read the stable bundle, bind matching tool handlers, and freeze the session. During greeting, perform only the exact frozen session-prefix prefill. | Stable bundle; finalized summary and bounded tail; user memory; date/time; session-prefix KV; greeting audio. |
| **3. First user turn** | Await the greeting-time prefill if unfinished, then use the same single inference slot for LLM/tools/TTS. Run rolling summaries only at the configured completed-turn threshold. | STT samples/features; frozen prompt plus current turn; active KV/activations; tool intermediates; TTS and playback buffers. |
| **4. Follow-up call starts -> before greeting ends** | Use exactly the Stage 2 path for the selected conversation. A hard reset may make model readiness colder, but no previous call-scoped state crosses the boundary. | The same categories as Stage 2 for the selected conversation and stable tool generation. |
| **5. Follow-up user turn** | Use exactly the Stage 3 path. Continuity comes from durable summary/history reconstructed into the frozen session, not retained turn state. | The same categories as Stage 3. |
| **6. Session breakdown** | Gate new turns, persist completed messages, close the session, publish a durable transition, and immediately clear call-scoped model/speech caches in bounded non-inference shutdown cleanup. Delete the fixed room promptly. Summary and prefix inference do not run in the outgoing job. | Release session KV, speech activations, allocator cache, and job-local inference/audio/tool objects. Preserve transcript, summary checkpoint, model parameters, and speech parameters after graceful teardown; rebuild model/runtime after hard reset. |

Only session-prefix cache loading may overlap a greeting. Recovery, model
loading, summarization, tool discovery, tool binding, and session construction
finish before greeting playback begins.

## State and Cache Boundaries

| State | Lifetime | Rule |
| --- | --- | --- |
| **Model/runtime foundation** | Service lifetime | Keep parameters, `mlx-lm`, kernels, and reusable buffers across graceful transitions. Rebuild after a model-server restart or explicit model change. |
| **Stable prompt bundle** | Until explicit invalidation | Stable system instructions, canonical tool schemas, language/model identity, metadata required for execution binding, and toolset fingerprint. Discover at Stage 1 or explicit reload—not ordinary calls. |
| **Stable prefix cache** | Until evicted/reset | Best-effort model cache for the exact stable bundle. Correctness never depends on a hit. Session prefill naturally recreates it after a miss. |
| **Session prefix** | One call | Stable bundle plus frozen date/time, user memory, finalized summary, and bounded tail. Build once before greeting; prefill during greeting. |
| **Turn state** | One turn | Current message, KV growth, activations, tool results, STT/TTS buffers. Release promptly. |
| **Durable conversation state** | Across all calls/resets | Full transcript, last valid summary, `summary_through_rowid`, and complete messages after that checkpoint. |

## Stable Tools

Service startup canonicalizes, sorts, deduplicates, fingerprints, persists, and
warms the model-visible schemas. Tool execution clients are separate. Each job
may open MCP connections and bind handlers, but it must verify that every
stable schema has a matching callable without rediscovering schemas.

An explicit tool reload creates a future stable generation:

```text
discover new schemas
  -> canonicalize and fingerprint
  -> atomically replace stable bundle
  -> warm new stable prefix
  -> future calls use it
```

The active call retains its frozen generation. It does not mutate schemas or
execution maps mid-call.

## Ending and Replacing a Session

There are exactly two memory-transition behaviors. Failed graceful recovery
escalates into hard reset; it is not a third behavior.

### Graceful behavior

Manual endings, conversation switches, and the MLX allocation boundary use
this behavior:

1. Refuse new turns.
2. Finish or cancel active inference and persist completed messages.
3. Close the AgentSession and publish a durable pending transition.
4. Let the outgoing shutdown callback cancel job-local tasks, clear local
   model/speech call caches, and close clients; it performs no model inference.
5. Delete the fixed LiveKit room promptly. Participant count is not a
   maintenance-completion signal because LiveKit disconnects the room before it
   awaits shutdown callbacks.
6. The replacement job repeats the idempotent prompt/speech cache clear as a
   fallback in case room deletion interrupted outgoing cleanup and, for a
   memory trip, checks recovery for no more than five seconds.
7. If recovery fails, enter hard reset. Otherwise continue to selected-session
   preparation.

### Hard reset

Critical macOS pressure restarts the supervised model process before ordinary
session cleanup. A failed graceful recovery invokes the same process restart.

1. Refuse new turns and interrupt the active model workload.
2. Restart the entire CAAL-owned model server so the request queue, generation
   thread, prompt caches, model references, and MLX allocator are reset.
3. Verify the local API. Model loading completes before greeting when the
   replacement job performs its first bounded preparation inference.
4. Continue to the same selected-session preparation. Expected recovery for the
   current local model is 10-20 seconds.

### Guard signals

| Signal | Behavior |
| --- | --- |
| MLX active allocation reaches 6 GiB by default | Graceful trip |
| macOS urgent memory pressure | Observe and log only; no trip |
| macOS critical memory pressure | Hard reset first |
| Model footprint reaches its cap while MLX metrics are unavailable | Graceful fallback trip |

MLX allocation comes from the owned server's `/v1/memory`. The process
footprint remains a fallback. Model-server prompt/decode concurrency defaults
to four, while each voice job serializes its own inference to one.

## Persistent Summary Maintenance

Messages are written as turns complete. Interrupted assistant output may remain
for display but is excluded from summarization and future model context.

During an active call, check pending characters after completed assistant turns.
At 4,000 characters by default, run one rolling checkpoint in the same inference
slot. A final user transcript cancels it so the live turn wins; failure retries
no more frequently than the configured cooldown.

Before a selected conversation's next greeting, checkpoint any remaining
eligible delta regardless of the rolling threshold. One request consumes at
most 6,000 characters:

```text
[previous summary]
[complete messages after summary_through_rowid]
```

On success, atomically replace the summary and advance the row checkpoint. On
failure or timeout, keep the prior checkpoint and load an unsummarized fallback
tail up to the same 6,000-character budget. Never reset again because summary
failed. A conversation switched away from need not be summarized until it is
selected again; its full transcript remains durable.

## Prompt Construction

| Prefix | Prepared in | Contents |
| --- | --- | --- |
| **Stable prefix** | Stage 1 or explicit invalidation | Exact stable system instructions and canonical tool definitions. |
| **Session prefix** | Stages 2 and 4 | Stable prefix plus finalized date/time, current user memory, selected summary, and bounded tail. Construct before greeting; prefill during greeting. |
| **Turn prefix** | Stages 3 and 5 | Frozen session prefix plus current-call turns and current user message. |

The exact token order is:

```text
[stable system instructions]
[exact stable tool definitions]
[session date/time]
[current user memory]
[finalized conversation summary]
[recent durable tail]
[current-call turns]
[current user message]
```

User-memory changes normally take effect in the next call. Greeting interruption
does not cancel session prefill; the first real user turn awaits the same task.
Cache state is reported outside model-visible history as `loading`, then `ready`
or `failed`.
