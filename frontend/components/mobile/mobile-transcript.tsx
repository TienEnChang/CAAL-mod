'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CheckIcon,
  MicrophoneIcon,
  MicrophoneSlashIcon,
  PencilSimpleIcon,
  PhoneCallIcon,
  PhoneDisconnectIcon,
  PlayPauseIcon,
  TrashIcon,
  XIcon,
} from '@phosphor-icons/react/dist/ssr';
import type { DesktopControlAction, MobileControlSnapshot } from '@/lib/mobile-control-store';

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
  toolStatus?: 'running' | 'complete' | 'failed';
  toolParams?: Record<string, unknown>[];
};

const OFFLINE_AFTER_MS = 25_000;
const CREATE_CONVERSATION_VALUE = '__create_conversation__';

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
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [mediaBusy, setMediaBusy] = useState(false);
  const [clock, setClock] = useState(() => Date.now());
  const transcriptRef = useRef<HTMLDivElement>(null);
  const loadedConversationRef = useRef<string | null>(null);
  const completedControlRef = useRef<string | null>(null);

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
      const nextSnapshot = JSON.parse(event.data) as MobileControlSnapshot;
      setSnapshot(nextSnapshot);
      const completedId = nextSnapshot.desktop.completedControlCommandId;
      if (completedId && completedId !== completedControlRef.current) {
        completedControlRef.current = completedId;
        void refreshConversations();
      }
      setError(null);
    };
    events.addEventListener('snapshot', handleSnapshot as EventListener);
    events.onerror = () => setError('Reconnecting to CAAL…');
    return () => events.close();
  }, [refreshConversations]);

  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);

  const activeConversationId = snapshot?.desktop.activeConversationId ?? storedActiveId;
  useEffect(() => {
    setEditingTitle(false);
    setConfirmingDelete(false);
  }, [activeConversationId]);

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
      const persistedToolParams = item.metadata.tool_params;
      byId.set(item.id, {
        id: item.id,
        role: item.role,
        content: item.content,
        createdAt: item.created_at * 1000,
        partial: false,
        toolStatus:
          item.role === 'tool'
            ? item.metadata.status === 'failed'
              ? 'failed'
              : 'complete'
            : undefined,
        toolParams:
          item.role === 'tool' && Array.isArray(persistedToolParams)
            ? (persistedToolParams as Record<string, unknown>[])
            : undefined,
      });
    }
    for (const item of snapshot?.desktop.liveMessages ?? []) {
      byId.set(item.id, {
        id: item.id,
        role: item.role,
        content: item.content,
        createdAt: item.createdAt,
        partial: item.partial,
        toolStatus: item.toolStatus,
        toolParams: item.toolParams,
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
  const desktopControllerReady = desktopOnline && snapshot?.desktop.controlProtocolVersion === 2;
  const desktopConnected = desktopOnline && Boolean(snapshot?.desktop.connected);
  const pendingConversationId = snapshot?.pendingConversationId ?? null;
  const pendingControlCommand = snapshot?.pendingControlCommand ?? null;
  const controlsBusy = Boolean(pendingConversationId || pendingControlCommand);
  const microphoneEnabled = Boolean(snapshot?.desktop.microphoneEnabled);
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

  const sendControl = async (
    action: DesktopControlAction,
    details: {
      microphoneEnabled?: boolean;
      conversationId?: string;
      conversationTitle?: string;
    } = {}
  ) => {
    try {
      await responseJson(
        await fetch('/api/mobile-control/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action, ...details }),
        })
      );
      setError(null);
      return true;
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to control CAAL');
      return false;
    }
  };

  const commitRename = async () => {
    const cleanedTitle = titleDraft.trim();
    if (!activeConversationId || !cleanedTitle) return;
    if (
      await sendControl('rename_conversation', {
        conversationId: activeConversationId,
        conversationTitle: cleanedTitle,
      })
    ) {
      setEditingTitle(false);
    }
  };

  const deleteActiveConversation = async () => {
    if (!activeConversationId) return;
    if (
      await sendControl('delete_conversation', {
        conversationId: activeConversationId,
      })
    ) {
      setConfirmingDelete(false);
    }
  };

  const toggleSystemMedia = async () => {
    setMediaBusy(true);
    try {
      await responseJson<{ ok: boolean }>(
        await fetch('/api/mobile-control/media', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'toggle' }),
        })
      );
      setError(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Unable to control system audio'
      );
    } finally {
      setMediaBusy(false);
    }
  };

  const pendingControlLabel =
    pendingControlCommand?.action === 'create_conversation'
      ? 'Creating session'
      : pendingControlCommand?.action === 'start_call'
        ? 'Starting call'
        : pendingControlCommand?.action === 'end_call'
          ? 'Ending call'
          : pendingControlCommand?.action === 'set_microphone_enabled'
            ? 'Updating microphone'
            : pendingControlCommand?.action === 'rename_conversation'
              ? 'Renaming session'
              : pendingControlCommand?.action === 'delete_conversation'
                ? 'Deleting session'
                : null;

  const statusLabel = !desktopOnline
    ? 'Desktop offline'
    : !desktopControllerReady
      ? 'Refresh desktop UI'
      : pendingConversationId
        ? 'Switching session'
        : pendingControlLabel
          ? pendingControlLabel
          : desktopConnected
            ? snapshot?.desktop.status || 'Connected'
            : 'Desktop disconnected';
  const visibleError = error ?? snapshot?.desktop.controlCommandError ?? null;
  const activeTitle =
    snapshot?.desktop.activeConversationTitle ??
    activeConversation?.title ??
    'No active conversation';

  return (
    <main className="bg-background text-foreground flex h-dvh flex-col overflow-hidden">
      <header className="border-border/70 bg-background/95 border-b px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-4 backdrop-blur">
        <div className="mx-auto max-w-2xl">
          <div className="flex items-center justify-between gap-4">
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.2em] uppercase">
              CAAL Mobile
            </p>
            <span className="border-border/70 bg-muted/40 rounded-full border px-3 py-1.5 font-mono text-[10px] tracking-wider uppercase">
              Controller
            </span>
          </div>

          {editingTitle ? (
            <form
              className="mt-1 flex items-center gap-1"
              onSubmit={(event) => {
                event.preventDefault();
                void commitRename();
              }}
            >
              <input
                autoFocus
                maxLength={64}
                aria-label="Session title"
                value={titleDraft}
                onChange={(event) => setTitleDraft(event.target.value)}
                className="border-input bg-muted/40 focus:border-primary min-w-0 flex-1 rounded-lg border px-2.5 py-1.5 text-base font-semibold outline-none"
              />
              <button
                type="submit"
                disabled={!titleDraft.trim() || controlsBusy}
                aria-label="Save session title"
                className="text-primary hover:bg-primary/10 rounded-lg p-2 disabled:opacity-40"
              >
                <CheckIcon className="size-4" weight="bold" />
              </button>
              <button
                type="button"
                aria-label="Cancel title edit"
                onClick={() => setEditingTitle(false)}
                className="text-muted-foreground hover:text-foreground rounded-lg p-2"
              >
                <XIcon className="size-4" />
              </button>
            </form>
          ) : (
            <div className="mt-1 flex min-w-0 items-center gap-1">
              <h1 className="min-w-0 flex-1 truncate text-lg font-semibold">{activeTitle}</h1>
              {confirmingDelete ? (
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    disabled={controlsBusy}
                    onClick={() => void deleteActiveConversation()}
                    className="rounded-lg px-2 py-1.5 text-xs font-semibold text-red-300 hover:bg-red-400/10 disabled:opacity-40"
                  >
                    Delete
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmingDelete(false)}
                    className="text-muted-foreground hover:text-foreground rounded-lg px-2 py-1.5 text-xs"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <div className="flex shrink-0 items-center">
                  <button
                    type="button"
                    disabled={!activeConversationId || !desktopControllerReady || controlsBusy}
                    aria-label={`Rename ${activeTitle}`}
                    onClick={() => {
                      setTitleDraft(activeTitle);
                      setEditingTitle(true);
                    }}
                    className="text-muted-foreground hover:text-foreground rounded-lg p-2 disabled:opacity-35"
                  >
                    <PencilSimpleIcon className="size-4" />
                  </button>
                  <button
                    type="button"
                    disabled={!activeConversationId || !desktopControllerReady || controlsBusy}
                    aria-label={`Delete ${activeTitle}`}
                    onClick={() => setConfirmingDelete(true)}
                    className="text-muted-foreground rounded-lg p-2 hover:text-red-300 disabled:opacity-35"
                  >
                    <TrashIcon className="size-4" />
                  </button>
                </div>
              )}
            </div>
          )}

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
            disabled={!desktopControllerReady || controlsBusy}
            onChange={(event) => {
              if (event.target.value === CREATE_CONVERSATION_VALUE) {
                void sendControl('create_conversation');
                return;
              }
              void selectConversation(event.target.value);
            }}
            className="border-input bg-muted/40 text-foreground w-full rounded-xl border px-3 py-3 text-sm outline-none disabled:opacity-50"
          >
            <option value={CREATE_CONVERSATION_VALUE}>＋ New session</option>
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
        <div className="mx-auto max-w-2xl space-y-4 pb-6">
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
                    {message.role === 'user'
                      ? 'You'
                      : message.role === 'tool'
                        ? `Tool · ${message.toolStatus ?? 'complete'}`
                        : 'CAAL'}
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
                {message.role === 'tool' && message.toolParams?.length ? (
                  <details className="border-border/50 mt-2 border-t pt-2">
                    <summary className="text-muted-foreground cursor-pointer font-mono text-[10px] tracking-wider uppercase">
                      Parameters
                    </summary>
                    <pre className="bg-background/50 text-muted-foreground mt-2 max-h-40 overflow-auto rounded-lg p-2 text-xs whitespace-pre-wrap">
                      {JSON.stringify(message.toolParams, null, 2)}
                    </pre>
                  </details>
                ) : null}
              </article>
            ))
          )}
        </div>
      </div>

      {visibleError && (
        <div className="border-border/70 bg-background/95 border-t px-4 py-2 text-center text-xs text-amber-300">
          {visibleError}
        </div>
      )}

      <footer className="border-border/70 bg-background/95 border-t px-4 pt-3 pb-[max(.75rem,env(safe-area-inset-bottom))] backdrop-blur">
        <div className="mx-auto grid max-w-2xl grid-cols-3 gap-2">
          <button
            type="button"
            disabled={mediaBusy}
            onClick={() => void toggleSystemMedia()}
            className="border-border bg-muted/40 hover:bg-muted/70 flex min-h-12 items-center justify-center gap-2 rounded-xl border px-2 text-xs font-semibold transition-colors disabled:cursor-wait disabled:opacity-45"
            aria-label="Pause or resume system media"
          >
            <PlayPauseIcon className="size-4 shrink-0" weight="fill" />
            <span>Pause / Resume</span>
          </button>

          <button
            type="button"
            disabled={!desktopControllerReady || !desktopConnected || controlsBusy}
            onClick={() =>
              void sendControl('set_microphone_enabled', {
                microphoneEnabled: !microphoneEnabled,
              })
            }
            className="border-border bg-muted/40 hover:bg-muted/70 flex min-h-12 items-center justify-center gap-2 rounded-xl border px-3 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45"
            aria-label={microphoneEnabled ? 'Mute desktop microphone' : 'Unmute desktop microphone'}
          >
            {microphoneEnabled ? (
              <MicrophoneIcon className="size-4 shrink-0" weight="bold" />
            ) : (
              <MicrophoneSlashIcon className="size-4 shrink-0" weight="bold" />
            )}
            <span>{microphoneEnabled ? 'Mute' : 'Unmute'}</span>
          </button>

          <button
            type="button"
            disabled={!desktopControllerReady || controlsBusy}
            onClick={() => void sendControl(desktopConnected ? 'end_call' : 'start_call')}
            className={`flex min-h-12 items-center justify-center gap-2 rounded-xl border px-3 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
              desktopConnected
                ? 'border-red-400/30 bg-red-400/10 text-red-300 hover:bg-red-400/20'
                : 'border-primary bg-primary text-primary-foreground hover:bg-primary/90'
            }`}
          >
            {desktopConnected ? (
              <PhoneDisconnectIcon className="size-4 shrink-0" weight="bold" />
            ) : (
              <PhoneCallIcon className="size-4 shrink-0" weight="bold" />
            )}
            <span>{desktopConnected ? 'End call' : 'Start call'}</span>
          </button>
        </div>
      </footer>
    </main>
  );
}
