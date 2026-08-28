'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Track } from 'livekit-client';
import {
  useSessionContext,
  useSessionMessages,
  useTranscriptions,
  useVoiceAssistant,
} from '@livekit/components-react';
import { useConversations } from '@/hooks/useConversations';
import { useSessionCache } from '@/hooks/useSessionCache';
import { useToolActivities } from '@/hooks/useToolStatus';
import { preferSystemDefaultMicrophone } from '@/lib/default-microphone';
import {
  getMicrophoneEnabledPreference,
  saveMicrophoneEnabledPreference,
  startSessionWithMicrophonePreference,
} from '@/lib/microphone-preference';
import { DESKTOP_CONTROL_PROTOCOL_VERSION } from '@/lib/mobile-control-store';
import type {
  ConversationSelectionCommand,
  DesktopControlCommand,
  DesktopControlState,
  MirroredTranscriptMessage,
  MobileControlSnapshot,
} from '@/lib/mobile-control-store';

const PUBLISH_INTERVAL_MS = 150;
const PUBLISH_STALE_AFTER_MS = 10_000;

export function DesktopMobileBridge() {
  const session = useSessionContext();
  const { state: agentState } = useVoiceAssistant();
  const sessionCache = useSessionCache();
  const { messages } = useSessionMessages(session);
  const transcriptions = useTranscriptions({ room: session.room });
  const {
    activeId,
    conversations,
    messages: persistedMessages,
    createConversation,
    deleteConversation,
    deleteMessage,
    renameConversation,
    selectConversation,
  } = useConversations();
  const toolActivities = useToolActivities(activeId);
  const [clientId] = useState(() => crypto.randomUUID());
  const [controlResult, setControlResult] = useState<{
    commandId: string | null;
    error: string | null;
  }>({ commandId: null, error: null });
  const switchingRef = useRef<string | null>(null);
  const controlInFlightRef = useRef<string | null>(null);
  const processedControlIdsRef = useRef(new Set<string>());
  const liveMessageCacheRef = useRef(
    new Map<string, { message: (typeof messages)[number]; partial: boolean }>()
  );
  const liveCacheConversationRef = useRef<string | null>(null);

  const activeConversation = conversations.find((item) => item.id === activeId);
  const agentReady =
    session.isConnected &&
    (agentState === 'listening' || agentState === 'thinking' || agentState === 'speaking');
  const microphonePublication = session.room.localParticipant.getTrackPublication(
    Track.Source.Microphone
  );
  const microphoneEnabled = session.isConnected
    ? Boolean(microphonePublication && !microphonePublication.isMuted)
    : getMicrophoneEnabledPreference();
  const persistedTranscriptRevision = activeId
    ? `${activeId}:${persistedMessages.length}:${persistedMessages.at(-1)?.id ?? ''}`
    : null;
  const partialIds = useMemo(
    () =>
      new Set(
        transcriptions
          .filter(
            (item) =>
              session.isConnected &&
              item.streamInfo.attributes?.['lk.transcription_final'] !== 'true'
          )
          .map((item) => item.streamInfo.id)
      ),
    [session.isConnected, transcriptions]
  );
  if (liveCacheConversationRef.current !== activeId || !session.isConnected) {
    liveMessageCacheRef.current.clear();
    liveCacheConversationRef.current = activeId;
  }
  for (const message of messages) {
    liveMessageCacheRef.current.set(message.id, {
      message,
      partial: partialIds.has(message.id),
    });
  }
  // Publish every retained live row, including ones this desktop has already
  // seen persisted. Only the phone knows when *its* copy of history has caught
  // up: it refetches over HTTP when the revision below changes, so dropping the
  // row here as soon as the desktop's own database read lands leaves the phone
  // with neither copy for a whole round trip, and the turn vanishes and returns.
  // The phone suppresses each row itself, against its own history, and holds a
  // tombstone for deletes; the cache is cleared per conversation and on
  // disconnect, and the payload stays capped by the slice below.
  const reconciledLiveMessages = [...liveMessageCacheRef.current.values()];

  const desktopState = useMemo<DesktopControlState>(
    () => ({
      controlProtocolVersion: DESKTOP_CONTROL_PROTOCOL_VERSION,
      clientId,
      connected: session.isConnected,
      agentReady,
      activeConversationId: activeId,
      activeConversationTitle: activeConversation?.title ?? null,
      status: session.isConnected ? agentState : 'disconnected',
      sessionCache,
      microphoneEnabled,
      completedControlCommandId: controlResult.commandId,
      controlCommandError: controlResult.error,
      persistedTranscriptRevision,
      liveMessages: [
        ...reconciledLiveMessages.map(({ message: item, partial }) => ({
          id: item.id,
          content: item.message,
          role: item.from?.isLocal ? ('user' as const) : ('assistant' as const),
          createdAt: item.timestamp,
          partial,
        })),
        ...toolActivities.map((activity) => {
          const status: NonNullable<MirroredTranscriptMessage['toolStatus']> =
            activity.status === 'complete'
              ? 'complete'
              : activity.status === 'failed'
                ? 'failed'
                : 'running';
          return {
            id: activity.id,
            content: `${
              status === 'running' ? 'Using' : status === 'failed' ? 'Failed to use' : 'Used'
            } ${activity.toolNames.map((name) => name.replace(/_/g, ' ')).join(', ')}`,
            role: 'tool' as const,
            createdAt: activity.timestamp,
            partial: false,
            toolStatus: status,
            toolParams: activity.toolParams,
          };
        }),
      ]
        .sort((a, b) => a.createdAt - b.createdAt)
        .slice(-50),
      updatedAt: Date.now(),
    }),
    [
      activeConversation?.title,
      activeId,
      agentReady,
      agentState,
      clientId,
      sessionCache,
      controlResult.commandId,
      controlResult.error,
      reconciledLiveMessages,
      microphoneEnabled,
      persistedTranscriptRevision,
      session.isConnected,
      toolActivities,
    ]
  );
  const latestStateRef = useRef(desktopState);
  latestStateRef.current = desktopState;
  const lastPublishedRef = useRef({ signature: '', at: 0 });
  // Everything except the timestamp, which changes on every render and would
  // make the state look new even when nothing about it is. Computed on the
  // tick rather than per render, so a burst of renders mid-sentence costs one
  // comparison rather than dozens.
  const stateSignature = useCallback(
    () => JSON.stringify({ ...latestStateRef.current, updatedAt: 0 }),
    []
  );

  const publishState = useCallback(async () => {
    lastPublishedRef.current = { signature: stateSignature(), at: Date.now() };
    try {
      await fetch('/api/mobile-control/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(latestStateRef.current),
        keepalive: true,
      });
    } catch {
      // Retry on the next tick once the frontend server returns.
      lastPublishedRef.current = { signature: '', at: 0 };
    }
  }, [stateSignature]);

  // Send on a steady rhythm rather than waiting for a quiet moment. The old
  // debounce restarted its timer on every change, and while you are speaking
  // the transcript changes continuously, so it never elapsed and the phone
  // froze until you paused. Ticking at a fixed rate and sending only when
  // something actually changed keeps updates flowing mid-sentence, and the
  // staleness fallback still refreshes an idle phone.
  useEffect(() => {
    const timer = setInterval(() => {
      const { signature, at } = lastPublishedRef.current;
      if (signature === stateSignature() && Date.now() - at < PUBLISH_STALE_AFTER_MS) return;
      void publishState();
    }, PUBLISH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [publishState, stateSignature]);

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

  const applyRemoteControl = useCallback(
    async (command: DesktopControlCommand) => {
      if (
        !command.commandId ||
        processedControlIdsRef.current.has(command.commandId) ||
        controlInFlightRef.current
      ) {
        return;
      }

      processedControlIdsRef.current.add(command.commandId);
      controlInFlightRef.current = command.commandId;
      let commandError: string | null = null;
      try {
        if (command.action === 'start_call') {
          if (!session.isConnected) {
            await startSessionWithMicrophonePreference(session);
          }
        } else if (command.action === 'end_call') {
          if (session.isConnected) await session.end();
        } else if (command.action === 'set_microphone_enabled') {
          const enabled = command.microphoneEnabled ?? false;
          saveMicrophoneEnabledPreference(enabled);
          if (session.isConnected) {
            if (enabled) {
              await preferSystemDefaultMicrophone(session.room);
            }
            await session.room.localParticipant.setMicrophoneEnabled(enabled);
          }
        } else if (command.action === 'create_conversation') {
          const created = await createConversation();
          if (!created) throw new Error('The desktop could not create a new session');
        } else if (command.action === 'rename_conversation') {
          const renamed = await renameConversation(
            command.conversationId ?? '',
            command.conversationTitle ?? ''
          );
          if (!renamed) throw new Error('The desktop could not rename this session');
        } else if (command.action === 'delete_conversation') {
          const deleted = await deleteConversation(command.conversationId ?? '');
          if (!deleted) throw new Error('The desktop could not delete this session');
        } else if (command.action === 'delete_message') {
          const deleted = await deleteMessage(command.messageId ?? '');
          if (!deleted) throw new Error('The desktop could not delete this message');
        }
      } catch (error) {
        commandError = error instanceof Error ? error.message : 'Desktop control failed';
      } finally {
        controlInFlightRef.current = null;
        setControlResult({ commandId: command.commandId, error: commandError });
      }
    },
    [createConversation, deleteConversation, deleteMessage, renameConversation, session]
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
      if (snapshot.pendingControlCommand) {
        void applyRemoteControl(snapshot.pendingControlCommand);
      }
    };
    const handleControl = (event: MessageEvent<string>) => {
      void applyRemoteControl(JSON.parse(event.data) as DesktopControlCommand);
    };
    events.addEventListener('select_conversation', handleSelection as EventListener);
    events.addEventListener('control_command', handleControl as EventListener);
    events.addEventListener('snapshot', handleSnapshot as EventListener);
    return () => events.close();
  }, [applyRemoteControl, applyRemoteSelection]);

  return null;
}
