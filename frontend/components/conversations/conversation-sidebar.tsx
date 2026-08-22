'use client';

import { useState } from 'react';
import { ListIcon, PlusIcon, TrashIcon, XIcon } from '@phosphor-icons/react/dist/ssr';
import { useConversations } from '@/hooks/useConversations';
import { cn } from '@/lib/utils';

export function ConversationLayout({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="grid h-svh min-h-0 grid-cols-1 overflow-hidden md:grid-cols-[280px_1fr]">
      <aside className="border-border bg-sidebar hidden min-h-0 border-r md:block">
        <ConversationSidebar />
      </aside>
      <div className="relative min-h-0 min-w-0 overflow-hidden">
        <button
          type="button"
          aria-label="Open conversations"
          onClick={() => setMobileOpen(true)}
          className="bg-background/80 border-border text-muted-foreground hover:text-foreground absolute top-4 left-4 z-[70] rounded-full border p-2 backdrop-blur md:hidden"
        >
          <ListIcon className="h-5 w-5" weight="bold" />
        </button>
        {children}
      </div>

      {mobileOpen && (
        <div className="fixed inset-0 z-[100] md:hidden">
          <button
            type="button"
            aria-label="Close conversations"
            className="absolute inset-0 bg-black/60"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="bg-sidebar border-border relative h-full w-[min(86vw,320px)] border-r shadow-2xl">
            <button
              type="button"
              aria-label="Close conversations"
              onClick={() => setMobileOpen(false)}
              className="text-muted-foreground hover:text-foreground absolute top-3 right-3 z-10 rounded-full p-2"
            >
              <XIcon className="h-5 w-5" />
            </button>
            <ConversationSidebar onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}
    </div>
  );
}

function ConversationSidebar({ onNavigate }: { onNavigate?: () => void }) {
  const {
    conversations,
    activeId,
    loading,
    error,
    createConversation,
    selectConversation,
    deleteConversation,
  } = useConversations();
  const [confirming, setConfirming] = useState<string | null>(null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-border border-b p-3">
        <button
          type="button"
          onClick={() => {
            void createConversation();
            onNavigate?.();
          }}
          className="border-border text-foreground hover:bg-sidebar-accent flex w-full items-center gap-2 rounded-lg border px-3 py-2.5 text-sm transition-colors"
        >
          <PlusIcon className="h-4 w-4" weight="bold" />
          New conversation
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {loading && <p className="text-muted-foreground px-3 py-2 text-sm">Loading…</p>}
        {error && <p className="px-3 py-2 text-xs text-red-400">{error}</p>}
        {!loading && conversations.length === 0 && (
          <p className="text-muted-foreground px-3 py-2 text-sm">No conversations yet.</p>
        )}

        {conversations.map((conversation) => (
          <div
            key={conversation.id}
            className={cn(
              'group relative mb-1 rounded-lg transition-colors',
              conversation.id === activeId ? 'bg-sidebar-accent' : 'hover:bg-sidebar-accent/60'
            )}
          >
            <button
              type="button"
              onClick={() => {
                void selectConversation(conversation.id);
                onNavigate?.();
              }}
              className="w-full rounded-lg px-3 py-2.5 pr-10 text-left"
            >
              <span className="text-sidebar-foreground block truncate text-sm">
                {conversation.title}
              </span>
              <span className="text-muted-foreground mt-1 block font-mono text-[11px]">
                {relativeTime(conversation.updated_at)} · {conversation.message_count} messages
              </span>
            </button>

            {confirming === conversation.id ? (
              <div className="bg-sidebar-accent absolute inset-y-1 right-1 flex items-center gap-1 rounded-md pl-2">
                <button
                  type="button"
                  className="rounded px-1.5 py-1 text-xs text-red-400 hover:bg-red-500/10"
                  onClick={() => {
                    void deleteConversation(conversation.id);
                    setConfirming(null);
                  }}
                >
                  Delete
                </button>
                <button
                  type="button"
                  className="text-muted-foreground rounded px-1.5 py-1 text-xs"
                  onClick={() => setConfirming(null)}
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                aria-label={`Delete ${conversation.title}`}
                onClick={() => setConfirming(conversation.id)}
                className="text-muted-foreground hover:text-foreground absolute top-2 right-2 rounded p-1.5 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
              >
                <TrashIcon className="h-4 w-4" />
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function relativeTime(timestamp: number) {
  const elapsedMinutes = Math.floor((Date.now() - timestamp * 1000) / 60_000);
  if (elapsedMinutes < 1) return 'now';
  if (elapsedMinutes < 60) return `${elapsedMinutes}m ago`;
  const hours = Math.floor(elapsedMinutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? 'yesterday' : `${days}d ago`;
}
