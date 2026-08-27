'use client';

import { useEffect, useState } from 'react';
import { RoomEvent } from 'livekit-client';
import { useRoomContext } from '@livekit/components-react';

export type SessionCacheState = 'loading' | 'ready' | 'failed';

/**
 * Track how far along the agent's prompt-cache warm-up is.
 *
 * The agent announces this on the "session_cache" topic, and repeats it
 * whenever a participant joins, so a client that connects after the warm-up
 * finished still learns the state. Null means nothing has been heard yet.
 */
export function useSessionCache(): SessionCacheState | null {
  const room = useRoomContext();
  const [state, setState] = useState<SessionCacheState | null>(null);

  useEffect(() => {
    if (!room) return;

    const handleData = (
      payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string
    ) => {
      if (topic !== 'session_cache') return;
      try {
        const data = JSON.parse(new TextDecoder().decode(payload)) as {
          status?: SessionCacheState;
        };
        if (data.status) setState(data.status);
      } catch {
        // A malformed status packet must not disturb the call.
      }
    };

    // Each call warms its own cache, so a new session starts from unknown.
    const handleReset = () => setState(null);

    room.on(RoomEvent.DataReceived, handleData);
    room.on(RoomEvent.Disconnected, handleReset);
    return () => {
      room.off(RoomEvent.DataReceived, handleData);
      room.off(RoomEvent.Disconnected, handleReset);
    };
  }, [room]);

  return state;
}
