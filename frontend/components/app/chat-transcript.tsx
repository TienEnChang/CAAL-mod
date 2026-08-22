'use client';

import { AnimatePresence, type HTMLMotionProps, motion } from 'motion/react';
import { type ReceivedMessage } from '@livekit/components-react';
import {
  CheckCircleIcon,
  SpinnerGapIcon,
  WarningCircleIcon,
  WrenchIcon,
} from '@phosphor-icons/react/dist/ssr';
import { ChatEntry } from '@/components/livekit/chat-entry';
import type { PersistedMessage } from '@/hooks/useConversations';
import type { ToolActivity } from '@/hooks/useToolStatus';
import { cn } from '@/lib/utils';

const MotionContainer = motion.create('div');
const MotionChatEntry = motion.create(ChatEntry);

const CONTAINER_MOTION_PROPS = {
  variants: {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: { delay: 0.2, duration: 0.3, staggerChildren: 0.1 },
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
};

const MESSAGE_MOTION_PROPS = {
  variants: {
    hidden: { opacity: 0, translateY: 10 },
    visible: { opacity: 1, translateY: 0 },
  },
};

interface ChatTranscriptProps {
  hidden?: boolean;
  messages?: ReceivedMessage[];
  historyMessages?: PersistedMessage[];
  partialIds?: Set<string>;
  toolActivities?: ToolActivity[];
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
    }
  | {
      kind: 'tool';
      id: string;
      timestamp: number;
      label: string;
      status: 'running' | 'complete' | 'failed';
    };

export function ChatTranscript({
  hidden = false,
  messages = [],
  historyMessages = [],
  partialIds = new Set(),
  toolActivities = [],
  ...props
}: ChatTranscriptProps & Omit<HTMLMotionProps<'div'>, 'ref'>) {
  const rows: TimelineRow[] = [
    ...historyMessages.map(
      (item): TimelineRow =>
        item.role === 'tool'
          ? {
              kind: 'tool',
              id: `history-${item.id}`,
              timestamp: item.created_at * 1000,
              label: item.content,
              status: item.metadata.status === 'failed' ? 'failed' : 'complete',
            }
          : {
              kind: 'message',
              id: `history-${item.id}`,
              timestamp: item.created_at * 1000,
              message: item.content,
              messageOrigin: item.role === 'user' ? 'local' : 'remote',
              partial: false,
              edited: false,
            }
    ),
    ...messages.map(
      (receivedMessage): TimelineRow => ({
        kind: 'message',
        id: receivedMessage.id,
        timestamp: receivedMessage.timestamp,
        message: receivedMessage.message,
        messageOrigin: receivedMessage.from?.isLocal ? 'local' : 'remote',
        partial: partialIds.has(receivedMessage.id),
        edited: receivedMessage.type === 'chatMessage' && !!receivedMessage.editTimestamp,
      })
    ),
    ...toolActivities.map(
      (activity): TimelineRow => ({
        kind: 'tool',
        id: activity.id,
        timestamp: activity.timestamp,
        label: `${
          activity.status === 'running'
            ? 'Using'
            : activity.status === 'failed'
              ? 'Failed to use'
              : 'Used'
        } ${activity.toolNames.map((name) => name.replace(/_/g, ' ')).join(', ')}`,
        status:
          activity.status === 'complete'
            ? 'complete'
            : activity.status === 'failed'
              ? 'failed'
              : 'running',
      })
    ),
  ].sort((a, b) => a.timestamp - b.timestamp);

  const locale = navigator?.language ?? 'en-US';

  return (
    <AnimatePresence>
      {!hidden && (
        <MotionContainer {...CONTAINER_MOTION_PROPS} {...props}>
          {rows.map((row) => {
            if (row.kind === 'tool') {
              return (
                <motion.div
                  key={row.id}
                  className="text-muted-foreground flex items-center gap-2 py-1 pl-1 font-mono text-xs"
                  {...MESSAGE_MOTION_PROPS}
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
                </motion.div>
              );
            }

            return (
              <MotionChatEntry
                key={row.id}
                locale={locale}
                timestamp={row.timestamp}
                message={row.message}
                messageOrigin={row.messageOrigin}
                hasBeenEdited={row.edited}
                aria-label={row.partial ? 'Partial transcription' : undefined}
                className={cn(
                  row.partial &&
                    'opacity-60 [&>span]:border [&>span]:border-dashed [&>span]:bg-transparent'
                )}
                {...MESSAGE_MOTION_PROPS}
              />
            );
          })}
        </MotionContainer>
      )}
    </AnimatePresence>
  );
}
