export interface FinalizedTranscriptRow {
  role: 'user' | 'assistant' | 'tool';
  createdAtMs: number;
}

export interface LiveTranscriptRow {
  id: string;
  role: 'user' | 'assistant' | 'tool';
  partial: boolean;
  createdAtMs: number;
}

/**
 * Return LiveKit rows superseded by a durable row from the same side.
 *
 * A final LiveKit stream can arrive before the matching database refresh. Keep
 * it visible until persistence catches up; otherwise the row disappears for a
 * render and then reappears as deletable. Persistence happens only when that
 * side's turn is complete, so a durable row at or after a live stream's
 * timestamp is a causal completion marker. No text matching is needed, and an
 * older repeated phrase cannot hide a new stream because its timestamp is
 * earlier.
 */
export function findSupersededLiveIds(
  history: readonly FinalizedTranscriptRow[],
  live: readonly LiveTranscriptRow[]
): Set<string> {
  let latestUserFinal = Number.NEGATIVE_INFINITY;
  let latestAssistantFinal = Number.NEGATIVE_INFINITY;
  for (const row of history) {
    if (row.role === 'user') latestUserFinal = Math.max(latestUserFinal, row.createdAtMs);
    if (row.role === 'assistant') {
      latestAssistantFinal = Math.max(latestAssistantFinal, row.createdAtMs);
    }
  }

  const superseded = new Set<string>();
  for (const row of live) {
    if (row.role === 'tool') continue;
    const latestFinal = row.role === 'user' ? latestUserFinal : latestAssistantFinal;
    if (latestFinal >= row.createdAtMs) superseded.add(row.id);
  }
  return superseded;
}
