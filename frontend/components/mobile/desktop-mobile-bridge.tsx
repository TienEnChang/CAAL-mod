'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  useSessionContext,
  useSessionMessages,
  useTranscriptions,
  useVoiceAssistant,
} from '@livekit/components-react';
import { useConversations } from '@/hooks/useConversations';
import type {
  ConversationSelectionCommand,
  DesktopControlState,
  MobileControlSnapshot,
} from '@/lib/mobile-control-store';

export function DesktopMobileBridge() {
  const session = useSessionContext();
  const { state: agentState } = useVoiceAssistant();
  const { messages } = useSessionMessages(session);
  const transcriptions = useTranscriptions({ room: session.room });
  const { activeId, conversations, selectConversation } = useConversations();
  const [clientId] = useState(() => crypto.randomUUID());
  const switchingRef = useRef<string | null>(null);

  const activeConversation = conversations.find((item) => item.id === activeId);
  const partialIds = useMemo(
    () =>
      new Set(
        transcriptions
          .filter(
            (item) =>
              session.isConnected &&
              item.participantInfo.identity === session.room.localParticipant.identity &&
              item.streamInfo.attributes?.['lk.transcription_final'] !== 'true'
          )
          .map((item) => item.streamInfo.id)
      ),
    [session.isConnected, session.room.localParticipant.identity, transcriptions]
  );

  const desktopState = useMemo<DesktopControlState>(
    () => ({
      clientId,
      connected: session.isConnected,
      activeConversationId: activeId,
      activeConversationTitle: activeConversation?.title ?? null,
      status: session.isConnected ? agentState : 'disconnected',
      liveMessages: messages.slice(-50).map((item) => ({
        id: item.id,
        content: item.message,
        role: item.from?.isLocal ? 'user' : 'assistant',
        createdAt: item.timestamp,
        partial: partialIds.has(item.id),
      })),
      updatedAt: Date.now(),
    }),
    [
      activeConversation?.title,
      activeId,
      agentState,
      clientId,
      messages,
      partialIds,
      session.isConnected,
    ]
  );
  const latestStateRef = useRef(desktopState);
  latestStateRef.current = desktopState;

  const publishState = useCallback(async () => {
    try {
      await fetch('/api/mobile-control/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(latestStateRef.current),
        keepalive: true,
      });
    } catch {
      // The heartbeat retries automatically when the frontend server returns.
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void publishState(), 100);
    return () => clearTimeout(timer);
  }, [desktopState, publishState]);

  useEffect(() => {
    const heartbeat = setInterval(() => void publishState(), 10_000);
    return () => clearInterval(heartbeat);
  }, [publishState]);

  const applyRemoteSelection = useCallback(
    async (conversationId: string) => {
      if (!conversationId || conversationId === activeId || switchingRef.current) return;
      switchingRef.current = conversationId;
      try {
        await selectConversation(conversationId);
      } finally {
        switchingRef.current = null;
        void publishState();
      }
    },
    [activeId, publishState, selectConversation]
  );

  useEffect(() => {
    const events = new EventSource('/api/mobile-control/events');
    const handleSelection = (event: MessageEvent<string>) => {
      const command = JSON.parse(event.data) as ConversationSelectionCommand;
      void applyRemoteSelection(command.conversationId);
    };
    const handleSnapshot = (event: MessageEvent<string>) => {
      const snapshot = JSON.parse(event.data) as MobileControlSnapshot;
      if (snapshot.pendingConversationId) {
        void applyRemoteSelection(snapshot.pendingConversationId);
      }
    };
    events.addEventListener('select_conversation', handleSelection as EventListener);
    events.addEventListener('snapshot', handleSnapshot as EventListener);
    return () => events.close();
  }, [applyRemoteSelection]);

  return null;
}
