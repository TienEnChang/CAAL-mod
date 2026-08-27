'use client';

import { useEffect, useState } from 'react';
import { RoomEvent } from 'livekit-client';
import { useRoomContext } from '@livekit/components-react';

export interface ToolStatus {
  id: string | null;
  toolUsed: boolean;
  toolNames: string[];
  toolParams: Record<string, unknown>[];
  status: 'idle' | 'running' | 'complete' | 'failed';
  timestamp: number;
}

export type ToolActivity = ToolStatus & { id: string };

/**
 * Hook to track tool usage status from the agent.
 * Listens for data packets with topic "tool_status" from the backend.
 */
export function useToolStatus() {
  const room = useRoomContext();
  const [toolStatus, setToolStatus] = useState<ToolStatus | null>(null);

  useEffect(() => {
    if (!room) return;

    const handleDataReceived = (
      payload: Uint8Array,
      participant: unknown,
      kind: unknown,
      topic?: string
    ) => {
      // Only handle tool_status messages
      if (topic !== 'tool_status') return;

      try {
        const decoder = new TextDecoder();
        const data = JSON.parse(decoder.decode(payload));

        setToolStatus({
          id: data.id ?? null,
          toolUsed: data.tool_used ?? false,
          toolNames: data.tool_names ?? [],
          toolParams: data.tool_params ?? [],
          status: data.status ?? (data.tool_used ? 'running' : 'idle'),
          timestamp: data.timestamp ?? Date.now(),
        });
      } catch (error) {
        console.error('[useToolStatus] Failed to parse tool status:', error);
      }
    };

    room.on(RoomEvent.DataReceived, handleDataReceived);

    return () => {
      room.off(RoomEvent.DataReceived, handleDataReceived);
    };
  }, [room]);

  return toolStatus;
}

/** Ordered tool activity rows for the transcript timeline. */
export function useToolActivities() {
  const room = useRoomContext();
  const [activities, setActivities] = useState<ToolActivity[]>([]);

  useEffect(() => {
    if (!room) return;
    // Hanging up only starts the history reload that replaces these rows with
    // their saved copies, so clearing here blanks a just-finished tool line out
    // until that request returns. Keep the finished ones — the transcript drops
    // each one as its saved copy arrives — and discard anything still running,
    // which can never finish now and would otherwise spin forever.
    const handleDisconnected = () =>
      setActivities((current) => current.filter((item) => item.status !== 'running'));
    // A new call starts from an empty list instead.
    const handleConnected = () => setActivities([]);
    const handleDataReceived = (
      payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string
    ) => {
      if (topic !== 'tool_status') return;
      try {
        const data = JSON.parse(new TextDecoder().decode(payload));
        if (!data.tool_used || !data.id) return;
        const activity: ToolActivity = {
          id: data.id,
          toolUsed: true,
          toolNames: data.tool_names ?? [],
          toolParams: data.tool_params ?? [],
          status: data.status ?? 'running',
          timestamp: data.timestamp ?? Date.now(),
        };
        setActivities((current) => {
          const index = current.findIndex((item) => item.id === activity.id);
          if (index < 0) return [...current, activity];
          const next = [...current];
          next[index] = { ...current[index], ...activity };
          return next;
        });
      } catch (error) {
        console.error('[useToolActivities] Failed to parse tool status:', error);
      }
    };
    room.on(RoomEvent.DataReceived, handleDataReceived);
    room.on(RoomEvent.Disconnected, handleDisconnected);
    room.on(RoomEvent.Connected, handleConnected);
    return () => {
      room.off(RoomEvent.DataReceived, handleDataReceived);
      room.off(RoomEvent.Disconnected, handleDisconnected);
      room.off(RoomEvent.Connected, handleConnected);
    };
  }, [room]);

  return activities;
}
