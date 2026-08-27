# CAAL Memory and Conversation Lifecycle

This document defines how CAAL prepares model memory, carries conversation meaning across calls, and recovers memory when a session ends. The design has four core operations mapped onto six user-visible stages:

```text
initialize model/runtime       Stage 1
prepare a call                 Stages 2 and 4
process a user turn            Stages 3 and 5
end, recover, summarize,
and restore stable prefix      Stage 6
```

Stages 2 and 4 must use the same preparation path. Stages 3 and 5 must use the same turn-processing path. The stage numbers describe when an operation occurs, not separate implementations.

## Lifecycle

| Stage | General principle | Memory occupants |
| --- | --- | --- |
| **1. Service startup -> ready for call** | Load expensive shared components once and perform representative inference before declaring readiness. Build the stable prompt prefix. Nothing call-specific should remain active. | Qwen model parameters; LM Studio/MLX runtime; compiled kernels and execution plans; allocator pools and reusable model buffers; STT and TTS model parameters; stable prefix containing system instructions and tool definitions. |
| **2. First call starts -> before greeting ends** | Run the shared call-preparation path. Use the greeting interval to assemble and prefill a fresh session prefix. Cancel preparation if the call ends, but not merely because the user interrupts the greeting. Record cache readiness as system activity outside model-visible history. | Stable prefix; session date/time; current user memory; existing summary and bounded recent tail when present; session-prefix KV/cache state; greeting audio and playback buffers. |
| **3. First user turn** | Run the shared turn-processing path. If greeting-time prefill is unfinished, await that same request before starting real inference so active Qwen concurrency remains one. Then allocate only the active STT -> Qwen/tools -> TTS working set and release each temporary component when it is no longer needed. After completed assistant turns, checkpoint only when the unsummarized transcript crosses the rolling threshold. | Captured and resampled audio; mel features and STT activations; current prompt; turn KV cache and Qwen activations; generated tokens and tool intermediates; TTS chunks, assembled audio, encoded WAV, and playback buffers. A threshold-triggered summary temporarily uses the same serialized inference slot. |
| **4. Follow-up call starts -> before greeting ends** | Run the same call-preparation path as Stage 2, including readiness reporting and barge-in behavior. Load the selected conversation's durable state after the previous transition has completed. A graceful transition leaves the model warm; a hard reset may make this stage physically as cold as Stage 2. | The same categories as Stage 2, using the current user memory and the selected conversation's latest valid summary and bounded tail. No active KV, audio, or tool state from the previous call may cross the boundary. |
| **5. Follow-up user turn** | Run exactly the same turn-processing and rolling-summary path as Stage 3, including awaiting unfinished prefix preparation. Continuity comes from the reconstructed session prefix, not retained inference state. | The same categories as Stage 3, built on the follow-up call's session prefix and current-call turns. |
| **6. Session breakdown** | Stop new turns, reclaim call-scoped memory through one of two transition modes, persist semantic state, summarize after clearance, and then explicitly rehydrate the stable prefix before reconnecting, switching, or remaining idle. | Release active KV and Qwen activations; STT/TTS/audio buffers; pending tool state; call-local prompt objects and session state. Preserve the transcript and summary checkpoint. Preserve model/runtime memory after a graceful transition; rebuild it after a hard reset. After the bounded summary request, warm the stable prefix containing the exact system instructions and tool definitions. |

## Memory Boundaries

CAAL must keep four kinds of state separate:

| State | Lifetime | Rule |
| --- | --- | --- |
| **Model/runtime foundation** | Service lifetime | Qwen parameters and the LM Studio/MLX runtime, kernels, execution plans, allocator pools, and reusable buffers load in Stage 1. Keep them across graceful transitions. Rebuild them only after a hard reset, LM Studio restart, or explicit model change. |
| **Stable prefix cache** | Until invalidated or evicted | Cached inference state for the exact system instructions and tool definitions. Create it in Stage 1 and explicitly rehydrate it in Stage 6 after summarization. A graceful transition may retain it, but CAAL must not assume that it survived teardown or the summary request. A hard reset always erases it. |
| **Call and turn inference state** | One call or one turn | Session and turn KV caches, activations, speech buffers, tool intermediates, and call-local objects must not cross a call boundary. |
| **Durable conversation state** | Across calls and process/model restarts | Conversation meaning lives in persistent history, summary, and checkpoint data. It must never depend on an LM Studio cache hit. |

LM Studio prompt caching is only a performance optimization. CAAL must remain correct when any prefix cache misses or is erased.

## Ending a Session

There are exactly two transition behaviors.

```text
                         +-> recovery succeeds -+
graceful teardown -------+                       |
                         +-> recovery fails -----+-> hard reset -+
                                                                  |
critical pressure -----------------------------> hard reset ------+
                                                                  |
                                      clearance/readiness succeeds
                                                                  |
                                            summarize durable delta
                                                                  |
                                           rehydrate stable prefix
                                                                  |
                                       reconnect, switch, or go idle
```

### Graceful transition

A manual ending, ordinary session switch, or normal model-footprint guard trip starts here:

1. Refuse new turns and new call-scoped allocations.
2. Finish or cancel active inference.
3. Persist all completed conversation messages.
4. Close the call and release Qwen turn state, STT/TTS state, audio, tool intermediates, and call-local objects.
5. For a memory-guard-triggered ending, verify that normal teardown reclaimed the required memory.
6. Preserve the model/runtime foundation. The stable prefix cache may survive, but do not rely on it.
7. If recovery fails, continue with the hard-reset procedure; otherwise continue to post-clearance summarization.

### Hard reset

Critical macOS memory pressure enters this path immediately. Failed graceful recovery escalates into the same path; escalation is not a third behavior.

1. Refuse new turns and new call-scoped allocations.
2. Restart or reload Qwen through LM Studio to reclaim memory. Under critical pressure this happens before ordinary session cleanup so enough memory exists to finish the procedure.
3. Complete session cleanup and persist all completed conversation messages.
4. Verify that the model and runtime are ready for inference.
5. Rebuild the model/runtime foundation. The stable prefix remains absent at this point.
6. Continue to the same post-clearance summarization used by the graceful path.

## Persistent History and Summary Maintenance

Each conversation persists this structure:

```text
full transcript
last valid summary
summary_through_rowid
complete messages after summary_through_rowid
```

Messages must be written as turns complete, rather than waiting for session teardown. An incomplete assistant response may be stored as interrupted for display, but it must be excluded from future context and summarization.

During an active call, check the unsummarized character count after each completed assistant turn. At 4,000 characters by default, run one bounded rolling-summary request. This threshold prevents per-turn summarization while keeping the pending delta below the normal 6,000-character summary input budget. Rolling maintenance shares the voice job's single inference lock. If a final user transcript arrives, cancel maintenance immediately and let the live turn proceed; retry only after a later assistant turn. A failed attempt is otherwise rate-limited for 120 seconds. Rolling updates affect only durable state for the next call and never rewrite the active call's session prefix.

At session breakdown, summarization also occurs after graceful recovery succeeds or hard-reset readiness is verified. This post-clearance pass checkpoints the remaining delta regardless of whether the rolling threshold was reached. It is not part of teardown, and it does not use an additional RAM threshold. Successful clearance/readiness is its only memory gate.

The bounded summary request consumes:

```text
[previous summary]
[complete messages after summary_through_rowid]
```

It must not repeatedly summarize the entire transcript. Limit its output tokens and execution time. On success, atomically replace the summary and advance the SQLite `summary_through_rowid` checkpoint. On failure or timeout, keep the previous summary and checkpoint, do not reset again, and do not block reconnection. The next call falls back to the previous summary plus a bounded recent tail.

After the summary request succeeds or terminates, explicitly run the stable-prefix warm-up with the exact production system instructions and tool definitions. This is required after both transition paths: it recreates the cache after a hard reset and validates or refreshes it after a graceful transition. Run it after summarization so the summary request cannot become the last inference workload and displace the prefix intended for the next call.

Reconnecting to the same session retains its conversation ID. Switching loads only the target conversation's durable state. Starting a new conversation creates empty history with no summary.

## Prompt Construction

The prompt has three nested prefix layers:

| Prefix | Prepared in | Contents |
| --- | --- | --- |
| **Stable prefix** | Stage 1 and Stage 6 | System instructions and tool definitions. Stage 6 explicitly rehydrates it after post-clearance summarization. |
| **Session prefix** | Stages 2 and 4 | Stable prefix, session date/time, current user memory, latest valid conversation summary, and bounded recent history. |
| **Turn prefix** | Stages 3 and 5 | Session prefix, completed turns from the current call, and the current user message. |

User memory belongs to the session prefix because it can change between calls and may be updated by the previous session.

Construct every prompt in this order:

```text
[system instructions]
[tool definitions]
[session date/time]
[current user memory]
[conversation summary]
[recent conversation tail]
[current-call turns]
[current user message]
```

Memory updates made during a call normally take effect in the next call's session prefix. If an update must take effect immediately, rebuild the current session prefix.

After a graceful transition, Stage 6 retains the model/runtime foundation and explicitly validates or refreshes the stable prefix. After a hard reset, it first restores model/runtime readiness and then recreates the stable prefix. In both cases, Stages 2 and 4 extend that stable prefix with user memory and durable conversation state to build a new session prefix.
