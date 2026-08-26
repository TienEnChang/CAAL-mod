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
import { useToolActivities } from '@/hooks/useToolStatus';
import { preferSystemDefaultMicrophone } from '@/lib/default-microphone';
import type {
  ConversationSelectionCommand,
  DesktopControlCommand,
  DesktopControlState,
  MirroredTranscriptMessage,
  MobileControlSnapshot,
} from '@/lib/mobile-control-store';

export function DesktopMobileBridge() {
  const session = useSessionContext();
  const { state: agentState } = useVoiceAssistant();
  const { messages } = useSessionMessages(session);
  const transcriptions = useTranscriptions({ room: session.room });
  const toolActivities = useToolActivities();
  const {
    activeId,
    conversations,
    createConversation,
    deleteConversation,
    renameConversation,
    selectConversation,
  } = useConversations();
  const [clientId] = useState(() => crypto.randomUUID());
  const [controlResult, setControlResult] = useState<{
    commandId: string | null;
    error: string | null;
  }>({ commandId: null, error: null });
  const switchingRef = useRef<string | null>(null);
  const controlInFlightRef = useRef<string | null>(null);
  const processedControlIdsRef = useRef(new Set<string>());

  const activeConversation = conversations.find((item) => item.id === activeId);
  const agentReady =
    session.isConnected &&
    (agentState === 'listening' || agentState === 'thinking' || agentState === 'speaking');
  const microphonePublication = session.room.localParticipant.getTrackPublication(
    Track.Source.Microphone
  );
  const microphoneEnabled = Boolean(
    session.isConnected && microphonePublication && !microphonePublication.isMuted
  );
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
      controlProtocolVersion: 2,
      clientId,
      connected: session.isConnected,
      agentReady,
      activeConversationId: activeId,
      activeConversationTitle: activeConversation?.title ?? null,
      status: session.isConnected ? agentState : 'disconnected',
      microphoneEnabled,
      completedControlCommandId: controlResult.commandId,
      controlCommandError: controlResult.error,
      liveMessages: [
        ...messages.map((item) => ({
          id: item.id,
          content: item.message,
          role: item.from?.isLocal ? ('user' as const) : ('assistant' as const),
          createdAt: item.timestamp,
          partial: partialIds.has(item.id),
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
      controlResult.commandId,
      controlResult.error,
      messages,
      microphoneEnabled,
      partialIds,
      session.isConnected,
      toolActivities,
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
            await preferSystemDefaultMicrophone(session.room);
            await session.start();
          }
        } else if (command.action === 'end_call') {
          if (session.isConnected) await session.end();
        } else if (command.action === 'set_microphone_enabled') {
          if (!session.isConnected) throw new Error('The desktop call is not connected');
          if (command.microphoneEnabled) {
            await preferSystemDefaultMicrophone(session.room);
          }
          await session.room.localParticipant.setMicrophoneEnabled(
            command.microphoneEnabled ?? false
          );
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
        }
      } catch (error) {
        commandError = error instanceof Error ? error.message : 'Desktop control failed';
      } finally {
        controlInFlightRef.current = null;
        setControlResult({ commandId: command.commandId, error: commandError });
      }
    },
    [createConversation, deleteConversation, renameConversation, session]
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
