'use client';

import { AnimatePresence, type HTMLMotionProps, motion } from 'motion/react';
import { type ReceivedMessage } from '@livekit/components-react';
import {
  CheckCircleIcon,
  SpinnerGapIcon,
  TrashIcon,
  WarningCircleIcon,
  WrenchIcon,
} from '@phosphor-icons/react/dist/ssr';
import { ChatEntry } from '@/components/livekit/chat-entry';
import type { PersistedMessage } from '@/hooks/useConversations';
import type { ToolActivity } from '@/hooks/useToolStatus';
import { cn } from '@/lib/utils';

const MotionContainer = motion.create('div');

const CONTAINER_MOTION_PROPS = {
  variants: {
    hidden: { opacity: 0 },
    visible: { opacity: 1, transition: { delay: 0.2, duration: 0.3 } },
  },
  animate: 'visible',
  exit: 'hidden',
};

interface ChatTranscriptProps {
  hidden?: boolean;
  messages?: ReceivedMessage[];
  historyMessages?: PersistedMessage[];
  partialIds?: Set<string>;
  suppressedLiveIds?: Set<string>;
  toolActivities?: ToolActivity[];
  /** Erase a stored turn so it stops reaching the model. */
  onDeleteMessage?: (messageId: string) => void;
}

type TimelineRow =
  | {
      kind: 'message';
      id: string;
      timestamp: number;
      message: string;
      messageOrigin: 'local' | 'remote';
      partial: boolean;
      edited: boolean;
      deletableId?: string;
    }
  | {
      kind: 'tool';
      id: string;
      timestamp: number;
      label: string;
      status: 'running' | 'complete' | 'failed';
    };

type ToolStatus = Extract<TimelineRow, { kind: 'tool' }>['status'];

const toolLabel = (status: ToolStatus, names: string[]) =>
  `${status === 'running' ? 'Using' : status === 'failed' ? 'Failed to use' : 'Used'} ${names
    .map((name) => name.replace(/_/g, ' '))
    .join(', ')}`;

export function ChatTranscript({
  hidden = false,
  messages = [],
  historyMessages = [],
  partialIds = new Set(),
  suppressedLiveIds = new Set(),
  toolActivities = [],
  onDeleteMessage,
  ...props
}: ChatTranscriptProps & Omit<HTMLMotionProps<'div'>, 'ref'>) {
  // One timeline keyed by id, the way the mobile viewer builds it. A saved row
  // and the live row it came from cannot share an id — the browser only ever
  // sees LiveKit's stream id, while the agent stores its own item id — so no
  // amount of matching makes the handoff a single continuous row. Nothing here
  // animates per row, which is what makes that harmless: a row swapping
  // identity underneath identical text is invisible.
  const rows = new Map<string, TimelineRow>();
  for (const item of historyMessages) {
    // Cache readiness is reported in the session status now. Older
    // conversations still hold rows from when it was a transcript entry.
    if (item.metadata.kind === 'session_cache') continue;
    rows.set(
      item.id,
      item.role === 'tool'
        ? {
            kind: 'tool',
            id: item.id,
            timestamp: item.created_at * 1000,
            label: item.content,
            status: item.metadata.status === 'failed' ? 'failed' : 'complete',
          }
        : {
            kind: 'message',
            id: item.id,
            timestamp: item.created_at * 1000,
            message: item.content,
            messageOrigin: item.role === 'user' ? 'local' : 'remote',
            partial: false,
            edited: false,
            deletableId: item.id,
          }
    );
  }
  // A live row stays visible through its final event until durable history
  // proves persistence caught up, so the turn never blinks out between the two.
  for (const receivedMessage of messages) {
    if (suppressedLiveIds.has(receivedMessage.id)) continue;
    rows.set(receivedMessage.id, {
      kind: 'message',
      id: receivedMessage.id,
      timestamp: receivedMessage.timestamp,
      message: receivedMessage.message,
      messageOrigin: receivedMessage.from?.isLocal ? 'local' : 'remote',
      partial: partialIds.has(receivedMessage.id),
      edited: receivedMessage.type === 'chatMessage' && !!receivedMessage.editTimestamp,
    });
  }
  // A tool row is saved under the very id its live status packet carried, so a
  // finished one collides with its saved copy here and the saved copy wins.
  // Tools still running have nothing saved yet and stay.
  for (const activity of toolActivities) {
    const status: ToolStatus =
      activity.status === 'complete'
        ? 'complete'
        : activity.status === 'failed'
          ? 'failed'
          : 'running';
    if (status !== 'running' && rows.has(activity.id)) continue;
    rows.set(activity.id, {
      kind: 'tool',
      id: activity.id,
      timestamp: activity.timestamp,
      label: toolLabel(status, activity.toolNames),
      status,
    });
  }
  const timeline = [...rows.values()].sort((a, b) => a.timestamp - b.timestamp);

  const locale = navigator?.language ?? 'en-US';

  // Sits in the row's own flow next to the timestamp rather than floating over
  // the bubble: a right-aligned user bubble would otherwise push an overlaid
  // control past its corner and its tooltip off the window edge.
  const deleteButton = (row: Extract<TimelineRow, { kind: 'message' }>) =>
    onDeleteMessage && row.deletableId ? (
      <button
        type="button"
        aria-label="Delete this message from history"
        onClick={() => onDeleteMessage(row.deletableId as string)}
        className="text-muted-foreground hover:text-destructive rounded p-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
      >
        <TrashIcon className="h-3.5 w-3.5" weight="bold" />
      </button>
    ) : null;

  return (
    <AnimatePresence>
      {!hidden && (
        <MotionContainer {...CONTAINER_MOTION_PROPS} {...props}>
          {timeline.map((row) => {
            if (row.kind === 'tool') {
              return (
                <div
                  key={row.id}
                  className="text-muted-foreground flex items-center gap-2 py-1 pl-1 font-mono text-xs"
                >
                  {row.status === 'running' ? (
                    <SpinnerGapIcon className="text-primary h-4 w-4 animate-spin" weight="bold" />
                  ) : row.status === 'failed' ? (
                    <WarningCircleIcon className="h-4 w-4 text-red-400" weight="fill" />
                  ) : (
                    <CheckCircleIcon className="text-primary h-4 w-4" weight="fill" />
                  )}
                  <WrenchIcon className="h-3.5 w-3.5" weight="bold" />
                  <span>{row.label}</span>
                </div>
              );
            }

            return (
              <ChatEntry
                key={row.id}
                locale={locale}
                timestamp={row.timestamp}
                message={row.message}
                messageOrigin={row.messageOrigin}
                hasBeenEdited={row.edited}
                action={deleteButton(row)}
                aria-label={row.partial ? 'Partial transcription' : undefined}
                className={cn(row.partial && '[&>span]:border-input [&>span]:border-dashed')}
              />
            );
          })}
        </MotionContainer>
      )}
    </AnimatePresence>
  );
}
