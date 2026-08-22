'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
  useSessionContext,
  useSessionMessages,
  useTranscriptions,
  useVoiceAssistant,
} from '@livekit/components-react';
import {
  ArrowDownIcon,
  BrainIcon,
  ChatTextIcon,
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
import { Button } from '@/components/livekit/button';
import { ScrollArea } from '@/components/livekit/scroll-area/scroll-area';
import { Toggle } from '@/components/livekit/toggle';
import { Tooltip } from '@/components/ui/tooltip';
import { useConversations } from '@/hooks/useConversations';
import { useToolActivities } from '@/hooks/useToolStatus';
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
    loading: conversationsLoading,
  } = useConversations();
  const toolActivities = useToolActivities();
  const [chatOpen, setChatOpen] = useState(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const shouldFollowLatestRef = useRef(true);

  const activeConversation = conversations.find((conversation) => conversation.id === activeId);
  const visibleMessages = session.isConnected ? messages : [];
  const visibleToolActivities = session.isConnected ? toolActivities : [];
  const timelineRows =
    historyMessages.length + visibleMessages.length + visibleToolActivities.length;
  const latestMessageText = visibleMessages.at(-1)?.message ?? '';
  const hasConversationContent = timelineRows > 0;

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

  useEffect(() => {
    setChatOpen(true);
  }, [activeId]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const area = scrollAreaRef.current;
      if (area) area.scrollTop = area.scrollHeight;
      shouldFollowLatestRef.current = true;
      setShowJumpToLatest(false);
    });
    return () => cancelAnimationFrame(frame);
  }, [activeId, historyMessages.length]);

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

  const statusLabel = !session.isConnected
    ? 'Disconnected'
    : agentState === 'listening'
      ? 'Listening'
      : agentState === 'thinking'
        ? 'Thinking'
        : agentState === 'speaking'
          ? 'Speaking'
          : agentState === 'connecting' || agentState === 'initializing'
            ? 'Connecting'
            : 'Connected';

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
            messages={visibleMessages}
            historyMessages={historyMessages}
            partialIds={partialIds}
            toolActivities={visibleToolActivities}
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
              <Tooltip content={tControlBar('transcript')}>
                <Toggle
                  size="icon"
                  variant="secondary"
                  aria-label={tControlBar('toggleTranscript')}
                  pressed={chatOpen}
                  onPressedChange={setChatOpen}
                >
                  <ChatTextIcon weight="bold" />
                </Toggle>
              </Tooltip>
              <Button
                variant="primary"
                size="lg"
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
