'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { MobileControlSnapshot } from '@/lib/mobile-control-store';

interface ConversationSummary {
  id: string;
  title: string;
  updated_at: number;
  message_count: number;
}

interface PersistedMessage {
  id: string;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  created_at: number;
  metadata: Record<string, unknown>;
}

type DisplayMessage = {
  id: string;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  createdAt: number;
  partial: boolean;
};

const OFFLINE_AFTER_MS = 25_000;

async function responseJson<T>(response: Response): Promise<T> {
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || body.detail || 'Request failed');
  return body as T;
}

export function MobileTranscript() {
  const [snapshot, setSnapshot] = useState<MobileControlSnapshot | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [storedActiveId, setStoredActiveId] = useState<string | null>(null);
  const [history, setHistory] = useState<PersistedMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const transcriptRef = useRef<HTMLDivElement>(null);
  const loadedConversationRef = useRef<string | null>(null);

  const refreshConversations = useCallback(async () => {
    const listing = await responseJson<{
      active_id: string;
      conversations: ConversationSummary[];
    }>(await fetch('/api/conversations', { cache: 'no-store' }));
    setConversations(listing.conversations);
    setStoredActiveId(listing.active_id);
  }, []);

  const loadConversation = useCallback(async (conversationId: string) => {
    const detail = await responseJson<{ messages: PersistedMessage[] }>(
      await fetch(`/api/conversations/${encodeURIComponent(conversationId)}`, {
        cache: 'no-store',
      })
    );
    loadedConversationRef.current = conversationId;
    setHistory(detail.messages);
  }, []);

  useEffect(() => {
    void Promise.all([
      refreshConversations(),
      fetch('/api/mobile-control/state', { cache: 'no-store' })
        .then(responseJson<MobileControlSnapshot>)
        .then(setSnapshot),
    ]).catch((requestError) => {
      setError(requestError instanceof Error ? requestError.message : 'Unable to load CAAL');
    });
  }, [refreshConversations]);

  useEffect(() => {
    const events = new EventSource('/api/mobile-control/events');
    const handleSnapshot = (event: MessageEvent<string>) => {
      setSnapshot(JSON.parse(event.data) as MobileControlSnapshot);
      setError(null);
    };
    events.addEventListener('snapshot', handleSnapshot as EventListener);
    events.onerror = () => setError('Reconnecting to CAAL…');
    return () => events.close();
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);

  const activeConversationId = snapshot?.desktop.activeConversationId ?? storedActiveId;
  useEffect(() => {
    if (!activeConversationId || loadedConversationRef.current === activeConversationId) return;
    void Promise.all([loadConversation(activeConversationId), refreshConversations()]).catch(
      (requestError) => {
        setError(
          requestError instanceof Error ? requestError.message : 'Unable to load conversation'
        );
      }
    );
  }, [activeConversationId, loadConversation, refreshConversations]);

  const messages = useMemo<DisplayMessage[]>(() => {
    const byId = new Map<string, DisplayMessage>();
    for (const item of history) {
      byId.set(item.id, {
        id: item.id,
        role: item.role,
        content: item.content,
        createdAt: item.created_at * 1000,
        partial: false,
      });
    }
    for (const item of snapshot?.desktop.liveMessages ?? []) {
      byId.set(item.id, {
        id: item.id,
        role: item.role,
        content: item.content,
        createdAt: item.createdAt,
        partial: item.partial,
      });
    }
    return [...byId.values()].sort((a, b) => a.createdAt - b.createdAt);
  }, [history, snapshot?.desktop.liveMessages]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const element = transcriptRef.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [messages]);

  const desktopOnline = Boolean(
    snapshot?.desktop.updatedAt && clock - snapshot.desktop.updatedAt < OFFLINE_AFTER_MS
  );
  const desktopConnected = desktopOnline && Boolean(snapshot?.desktop.connected);
  const pendingConversationId = snapshot?.pendingConversationId ?? null;
  const activeConversation = conversations.find((item) => item.id === activeConversationId);

  const selectConversation = async (conversationId: string) => {
    if (!conversationId || conversationId === activeConversationId) return;
    const conversation = conversations.find((item) => item.id === conversationId);
    try {
      await responseJson(
        await fetch('/api/mobile-control/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conversationId,
            conversationTitle: conversation?.title ?? null,
          }),
        })
      );
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to switch session');
    }
  };

  const statusLabel = !desktopOnline
    ? 'Desktop offline'
    : pendingConversationId
      ? 'Switching session'
      : desktopConnected
        ? snapshot?.desktop.status || 'Connected'
        : 'Desktop disconnected';

  return (
    <main className="bg-background text-foreground flex h-dvh flex-col overflow-hidden">
      <header className="border-border/70 bg-background/95 border-b px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-4 backdrop-blur">
        <div className="mx-auto flex max-w-2xl items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.2em] uppercase">
              CAAL Mobile
            </p>
            <h1 className="mt-1 truncate text-lg font-semibold">
              {snapshot?.desktop.activeConversationTitle ??
                activeConversation?.title ??
                'No active conversation'}
            </h1>
            <div className="text-muted-foreground mt-1 flex items-center gap-2 text-xs capitalize">
              <span
                className={`size-2 rounded-full ${
                  desktopConnected
                    ? 'bg-primary'
                    : desktopOnline
                      ? 'bg-amber-400'
                      : 'bg-muted-foreground/40'
                }`}
              />
              {statusLabel}
            </div>
          </div>
          <span className="border-border/70 bg-muted/40 rounded-full border px-3 py-1.5 font-mono text-[10px] tracking-wider uppercase">
            Viewer
          </span>
        </div>

        <div className="mx-auto mt-4 max-w-2xl">
          <label
            htmlFor="mobile-conversation"
            className="text-muted-foreground mb-1.5 block font-mono text-[10px] tracking-wider uppercase"
          >
            Desktop WebRTC conversation
          </label>
          <select
            id="mobile-conversation"
            value={pendingConversationId ?? activeConversationId ?? ''}
            disabled={!desktopOnline || Boolean(pendingConversationId)}
            onChange={(event) => void selectConversation(event.target.value)}
            className="border-input bg-muted/40 text-foreground w-full rounded-xl border px-3 py-3 text-sm outline-none disabled:opacity-50"
          >
            {!activeConversationId && <option value="">Select a conversation</option>}
            {conversations.map((conversation) => (
              <option key={conversation.id} value={conversation.id}>
                {conversation.title} · {conversation.message_count} messages
              </option>
            ))}
          </select>
          {pendingConversationId && (
            <p className="text-primary mt-2 text-xs">
              Desktop is reconnecting to the selected conversation…
            </p>
          )}
        </div>
      </header>

      <div ref={transcriptRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <div className="mx-auto max-w-2xl space-y-4 pb-[max(1.5rem,env(safe-area-inset-bottom))]">
          {messages.length === 0 ? (
            <div className="text-muted-foreground grid min-h-[45vh] place-content-center text-center">
              <div>
                <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full border border-current/15">
                  <span className="font-mono text-lg">•••</span>
                </div>
                <p className="text-foreground font-medium">Waiting for transcript</p>
                <p className="mt-1 text-sm">The Mac remains the microphone and WebRTC client.</p>
              </div>
            </div>
          ) : (
            messages.map((message) => (
              <article
                key={message.id}
                className={`rounded-2xl border px-4 py-3 ${
                  message.role === 'user'
                    ? 'border-primary/25 bg-primary/10 ml-5'
                    : message.role === 'tool'
                      ? 'border-border/60 bg-muted/20 mx-3'
                      : 'border-border/60 bg-muted/40 mr-5'
                } ${message.partial ? 'border-dashed opacity-65' : ''}`}
              >
                <div className="text-muted-foreground mb-1.5 flex items-center justify-between gap-3 font-mono text-[10px] tracking-wider uppercase">
                  <span>
                    {message.role === 'user' ? 'You' : message.role === 'tool' ? 'Tool' : 'CAAL'}
                    {message.partial ? ' · listening' : ''}
                  </span>
                  <time>
                    {new Intl.DateTimeFormat(undefined, {
                      hour: 'numeric',
                      minute: '2-digit',
                    }).format(message.createdAt)}
                  </time>
                </div>
                <p className="text-[15px] leading-relaxed whitespace-pre-wrap">{message.content}</p>
              </article>
            ))
          )}
        </div>
      </div>

      {error && (
        <div className="border-border/70 bg-background/95 border-t px-4 py-2 text-center text-xs text-amber-300">
          {error}
        </div>
      )}
    </main>
  );
}
