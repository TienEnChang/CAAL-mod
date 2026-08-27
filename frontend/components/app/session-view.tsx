'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Track } from 'livekit-client';
import {
  useSessionContext,
  useSessionMessages,
  useTranscriptions,
  useVoiceAssistant,
} from '@livekit/components-react';
import type { ReceivedMessage } from '@livekit/components-react';
import {
  ArrowDownIcon,
  BrainIcon,
  GearIcon,
  WaveformIcon,
  WrenchIcon,
} from '@phosphor-icons/react/dist/ssr';
import type { AppConfig } from '@/app-config';
import { ChatTranscript } from '@/components/app/chat-transcript';
import { PreConnectMessage } from '@/components/app/preconnect-message';
import { TileLayout } from '@/components/app/tile-layout';
import {
  AgentControlBar,
  type ControlBarControls,
} from '@/components/livekit/agent-control-bar/agent-control-bar';
import { TrackSelector } from '@/components/livekit/agent-control-bar/track-selector';
import { Button } from '@/components/livekit/button';
import { ScrollArea } from '@/components/livekit/scroll-area/scroll-area';
import { Tooltip } from '@/components/ui/tooltip';
import { useConversations } from '@/hooks/useConversations';
import { useSessionCache } from '@/hooks/useSessionCache';
import { useToolActivities } from '@/hooks/useToolStatus';
import {
  MICROPHONE_PREFERENCE_CHANGED_EVENT,
  getMicrophoneEnabledPreference,
  saveMicrophoneEnabledPreference,
} from '@/lib/microphone-preference';
import { findSupersededLiveIds } from '@/lib/transcript-finalization';
import { cn } from '@/lib/utils';

interface FadeProps {
  top?: boolean;
  bottom?: boolean;
  className?: string;
}

export function Fade({ top = false, bottom = false, className }: FadeProps) {
  return (
    <div
      className={cn(
        'from-background pointer-events-none h-4 bg-linear-to-b to-transparent',
        top && 'bg-linear-to-b',
        bottom && 'bg-linear-to-t',
        className
      )}
    />
  );
}

interface SessionViewProps {
  appConfig: AppConfig;
  onStartCall: () => void;
  onOpenSettings?: () => void;
  onOpenTools?: () => void;
  onOpenMemory?: () => void;
}

function IdleVoiceState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="pointer-events-none absolute inset-x-6 top-[67px] bottom-32 flex items-center justify-center md:bottom-40">
      <div className="text-muted-foreground flex max-w-sm flex-col items-center text-center">
        <div className="border-border/60 bg-muted/20 mb-4 rounded-full border p-5">
          <WaveformIcon className="text-foreground/80 h-12 w-12" weight="bold" />
        </div>
        <p className="text-foreground text-base font-medium">{title}</p>
        <p className="mt-1.5 text-xs">{detail}</p>
      </div>
    </div>
  );
}

export const SessionView = ({
  appConfig,
  onStartCall,
  onOpenSettings,
  onOpenTools,
  onOpenMemory,
  ...props
}: React.ComponentProps<'section'> & SessionViewProps) => {
  const tWelcome = useTranslations('Welcome');
  const tCommon = useTranslations('Common');
  const tControlBar = useTranslations('ControlBar');
  const session = useSessionContext();
  const { state: agentState } = useVoiceAssistant();
  const { messages } = useSessionMessages(session);
  const transcriptions = useTranscriptions({ room: session.room });
  const {
    activeId,
    conversations,
    messages: historyMessages,
    deleteMessage,
    loading: conversationsLoading,
  } = useConversations();
  const toolActivities = useToolActivities();
  const [chatOpen, setChatOpen] = useState(true);
  const [microphoneEnabledPreference, setMicrophoneEnabledPreference] = useState(() =>
    getMicrophoneEnabledPreference()
  );
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const shouldFollowLatestRef = useRef(true);
  const [deletedLiveIds, setDeletedLiveIds] = useState<ReadonlySet<string>>(new Set());
  const liveMessageCacheRef = useRef(
    new Map<string, { message: ReceivedMessage; partial: boolean }>()
  );
  const liveCacheConversationRef = useRef<string | null>(null);
  const wasConnectedRef = useRef(session.isConnected);

  // Tombstones outlive the hangup for the same reason the live rows do: they
  // are what stops a deleted turn reappearing while history reloads. A fresh
  // call starts clean.
  useEffect(() => {
    if (session.isConnected) setDeletedLiveIds(new Set());
  }, [activeId, session.isConnected]);

  const activeConversation = conversations.find((conversation) => conversation.id === activeId);
  const visibleMessages = messages;
  // Kept past the hangup for the same reason the messages are: the hook now
  // retires running rows itself, and the transcript drops each finished one as
  // its saved copy arrives.
  const visibleToolActivities = toolActivities;
  const latestMessageText = visibleMessages.at(-1)?.message ?? '';

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
  // LiveKit removes finalized transcription streams before the conversation
  // database refresh reaches the browser. Retain each stream locally until a
  // durable row (or a deletion tombstone) proves it can be removed.
  const justConnected = session.isConnected && !wasConnectedRef.current;
  wasConnectedRef.current = session.isConnected;
  // Cleared when the conversation changes or a new call starts, not when one
  // ends. Hanging up triggers a history refresh, and dropping the live rows the
  // moment the socket closes blanked the final turn out until that HTTP round
  // trip returned. Suppression below retires each row on its own evidence.
  if (liveCacheConversationRef.current !== activeId || justConnected) {
    liveMessageCacheRef.current.clear();
    liveCacheConversationRef.current = activeId;
  }
  for (const message of visibleMessages) {
    liveMessageCacheRef.current.set(message.id, {
      message,
      partial: partialIds.has(message.id),
    });
  }
  // An utterance still mid-transcription when the call drops never finalizes
  // and never persists, so nothing downstream would ever retire it. Only
  // finalized rows are worth holding past the hangup.
  if (!session.isConnected) {
    for (const [id, entry] of liveMessageCacheRef.current) {
      if (entry.partial) liveMessageCacheRef.current.delete(id);
    }
  }
  const cachedLiveMessages = [...liveMessageCacheRef.current.values()];
  const cachedPartialIds = new Set(
    cachedLiveMessages.filter((item) => item.partial).map((item) => item.message.id)
  );
  const reconciledMessages = cachedLiveMessages.map((item) => item.message);
  const supersededLiveIds = findSupersededLiveIds(
    historyMessages.map((message) => ({
      role: message.role,
      createdAtMs: message.created_at * 1000,
    })),
    cachedLiveMessages.map(({ message, partial }) => ({
      id: message.id,
      role: message.from?.isLocal ? 'user' : 'assistant',
      partial,
      createdAtMs: message.timestamp,
    }))
  );
  for (const id of new Set([...supersededLiveIds, ...deletedLiveIds])) {
    liveMessageCacheRef.current.delete(id);
  }
  const suppressedLiveIds = useMemo(
    () => new Set([...supersededLiveIds, ...deletedLiveIds]),
    [deletedLiveIds, supersededLiveIds]
  );
  const handleDeleteMessage = useCallback(
    (messageId: string) => {
      // Once the durable row disappears it can no longer suppress LiveKit's
      // stale partial. Preserve every currently established completion as a
      // call-local tombstone before applying the optimistic delete.
      setDeletedLiveIds((current) => new Set([...current, ...supersededLiveIds]));
      void deleteMessage(messageId);
    },
    [deleteMessage, supersededLiveIds]
  );
  const timelineRows =
    historyMessages.length +
    reconciledMessages.filter((message) => !suppressedLiveIds.has(message.id)).length +
    visibleToolActivities.length;
  const hasConversationContent = timelineRows > 0;
  const preCallMicrophoneTrackRef = useMemo(
    () => ({
      participant: session.room.localParticipant,
      source: Track.Source.Microphone,
      publication: session.room.localParticipant.getTrackPublication(Track.Source.Microphone),
    }),
    [session.room.localParticipant]
  );

  const controls: ControlBarControls = {
    leave: true,
    microphone: true,
    chat: appConfig.supportsChatInput,
    camera: appConfig.supportsVideoInput,
    screenShare: appConfig.supportsVideoInput,
  };

  const scrollToLatest = useCallback(() => {
    const area = scrollAreaRef.current;
    if (!area) return;
    area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
    shouldFollowLatestRef.current = true;
    setShowJumpToLatest(false);
  }, []);

  const updateScrollPosition = useCallback(() => {
    const area = scrollAreaRef.current;
    if (!area) return;
    const distanceFromBottom = area.scrollHeight - area.clientHeight - area.scrollTop;
    const isNearBottom = distanceFromBottom <= 120;
    shouldFollowLatestRef.current = isNearBottom;
    setShowJumpToLatest(!isNearBottom);
  }, []);

  const updateMicrophonePreference = useCallback((enabled: boolean) => {
    saveMicrophoneEnabledPreference(enabled);
    setMicrophoneEnabledPreference(enabled);
  }, []);

  useEffect(() => {
    setChatOpen(true);
  }, [activeId]);

  useEffect(() => {
    const syncMicrophonePreference = (event: Event) => {
      setMicrophoneEnabledPreference((event as CustomEvent<boolean>).detail);
    };
    window.addEventListener(MICROPHONE_PREFERENCE_CHANGED_EVENT, syncMicrophonePreference);
    return () =>
      window.removeEventListener(MICROPHONE_PREFERENCE_CHANGED_EVENT, syncMicrophonePreference);
  }, []);

  // Jump to the bottom when you open a conversation, and only then. Following
  // the newest turn afterwards is the next effect's job, and it follows only
  // while you are already reading at the bottom. Firing this on every saved
  // turn as well used to drag you back down mid-call each time one persisted.
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const area = scrollAreaRef.current;
      if (area) area.scrollTop = area.scrollHeight;
      shouldFollowLatestRef.current = true;
      setShowJumpToLatest(false);
    });
    return () => cancelAnimationFrame(frame);
  }, [activeId]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const area = scrollAreaRef.current;
      if (!area) return;
      if (shouldFollowLatestRef.current) {
        area.scrollTop = area.scrollHeight;
        setShowJumpToLatest(false);
      } else {
        updateScrollPosition();
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [latestMessageText, timelineRows, updateScrollPosition]);

  // Until the first thing you say, the agent state only reflects the canned
  // greeting playing out, which says nothing useful. Report the prompt-cache
  // warm-up instead, since that is what the session is actually waiting on.
  const sessionCache = useSessionCache();
  const hasUserSpoken = visibleMessages.some((message) => message.from?.isLocal);
  const cacheLabel =
    session.isConnected && !hasUserSpoken && sessionCache
      ? sessionCache === 'loading'
        ? 'Session cache loading'
        : sessionCache === 'ready'
          ? 'Ready'
          : 'Session cache unavailable'
      : null;

  const statusLabel = !session.isConnected
    ? 'Disconnected'
    : (cacheLabel ??
      (agentState === 'listening'
        ? 'Listening'
        : agentState === 'thinking'
          ? 'Thinking'
          : agentState === 'speaking'
            ? 'Speaking'
            : agentState === 'connecting' || agentState === 'initializing'
              ? 'Connecting'
              : 'Connected'));

  return (
    <section className="bg-background relative z-10 h-full w-full overflow-hidden" {...props}>
      <header className="border-border/60 bg-background/90 absolute inset-x-0 top-0 z-[60] flex h-[67px] items-center border-b px-4 pl-16 backdrop-blur md:px-6">
        <div className="min-w-0">
          <p className="text-foreground truncate text-sm font-medium">
            {activeConversation?.title ?? 'New conversation'}
          </p>
          <div className="text-muted-foreground mt-0.5 flex items-center gap-1.5 font-mono text-[11px] tracking-wider uppercase">
            <span
              className={cn(
                'size-1.5 rounded-full',
                session.isConnected ? 'bg-primary' : 'bg-muted-foreground/50'
              )}
            />
            {statusLabel}
          </div>
        </div>

        <div className="ml-auto flex items-center gap-1">
          {onOpenMemory && (
            <Tooltip content={tCommon('memory')}>
              <button
                type="button"
                onClick={onOpenMemory}
                className="text-muted-foreground hover:text-foreground hover:bg-muted rounded-full p-2 transition-colors"
              >
                <BrainIcon className="h-5 w-5" weight="fill" />
              </button>
            </Tooltip>
          )}
          {onOpenTools && (
            <Tooltip content={tCommon('tools')}>
              <button
                type="button"
                onClick={onOpenTools}
                className="text-muted-foreground hover:text-foreground hover:bg-muted rounded-full p-2 transition-colors"
              >
                <WrenchIcon className="h-5 w-5" weight="fill" />
              </button>
            </Tooltip>
          )}
          {onOpenSettings && (
            <Tooltip content={tCommon('settings')}>
              <button
                type="button"
                onClick={onOpenSettings}
                className="text-muted-foreground hover:text-foreground hover:bg-muted rounded-full p-2 transition-colors"
              >
                <GearIcon className="h-5 w-5" weight="bold" />
              </button>
            </Tooltip>
          )}
        </div>
      </header>

      <div
        className={cn(
          'absolute inset-0 grid grid-cols-1 grid-rows-1',
          !chatOpen && 'pointer-events-none'
        )}
      >
        <Fade top className="absolute inset-x-4 top-16 z-10 h-20" />
        <ScrollArea
          ref={scrollAreaRef}
          onScroll={updateScrollPosition}
          className={cn(
            'px-4 pb-[150px] md:px-6 md:pb-[190px]',
            session.isConnected ? 'pt-48 md:pt-52' : 'pt-24'
          )}
        >
          <ChatTranscript
            hidden={!chatOpen}
            messages={reconciledMessages}
            historyMessages={historyMessages}
            partialIds={cachedPartialIds}
            suppressedLiveIds={suppressedLiveIds}
            toolActivities={visibleToolActivities}
            onDeleteMessage={handleDeleteMessage}
            className="mx-auto max-w-2xl space-y-3 transition-opacity duration-300 ease-out"
          />
        </ScrollArea>

        {chatOpen && !hasConversationContent && !conversationsLoading && (
          <IdleVoiceState title={tWelcome('subtitle')} detail="Transcript On" />
        )}
      </div>

      {!chatOpen && !session.isConnected && (
        <IdleVoiceState title={tWelcome('subtitle')} detail="Transcript Off" />
      )}

      {session.isConnected && <TileLayout chatOpen={chatOpen} />}

      {chatOpen && showJumpToLatest && (
        <Button
          type="button"
          size="sm"
          variant="secondary"
          onClick={scrollToLatest}
          className="absolute right-4 bottom-28 z-[55] rounded-full shadow-lg md:right-8 md:bottom-36"
        >
          <ArrowDownIcon weight="bold" />
          Jump to latest
        </Button>
      )}

      <div className="absolute inset-x-3 bottom-0 z-50 md:inset-x-12">
        {session.isConnected ? (
          <>
            {appConfig.isPreConnectBufferEnabled && (
              <PreConnectMessage messages={messages} className="pb-4" />
            )}
            <div className="bg-background relative mx-auto max-w-2xl pb-3 md:pb-12">
              <Fade bottom className="absolute inset-x-0 top-0 h-4 -translate-y-full" />
              <AgentControlBar
                controls={controls}
                isConnected
                transcriptOpen={chatOpen}
                onDisconnect={session.end}
                onChatOpenChange={setChatOpen}
              />
            </div>
          </>
        ) : (
          <div className="bg-background relative mx-auto max-w-2xl pb-3 md:pb-12">
            <Fade bottom className="absolute inset-x-0 top-0 h-4 -translate-y-full" />
            <div className="border-input/50 dark:border-muted flex items-center gap-2 rounded-[31px] border p-3 drop-shadow-md/3">
              <TrackSelector
                kind="audioinput"
                aria-label={tControlBar('toggleMicrophone')}
                source={Track.Source.Microphone}
                pressed={microphoneEnabledPreference}
                audioTrackRef={preCallMicrophoneTrackRef}
                onPressedChange={updateMicrophonePreference}
              />
              <Button
                variant="primary"
                onClick={onStartCall}
                className="grow rounded-full font-mono"
              >
                <WaveformIcon weight="bold" />
                {tWelcome('start')}
              </Button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
};
