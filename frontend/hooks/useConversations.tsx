'use client';

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { RoomEvent } from 'livekit-client';
import { useSessionContext } from '@livekit/components-react';
import { startSessionWithMicrophonePreference } from '@/lib/microphone-preference';

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
}

export interface PersistedMessage {
  id: string;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  metadata: Record<string, unknown>;
  created_at: number;
}

interface ConversationContextValue {
  conversations: ConversationSummary[];
  activeId: string | null;
  messages: PersistedMessage[];
  loading: boolean;
  error: string | null;
  createConversation: () => Promise<boolean>;
  selectConversation: (id: string) => Promise<void>;
  renameConversation: (id: string, title: string) => Promise<boolean>;
  deleteConversation: (id: string) => Promise<boolean>;
  refresh: () => Promise<void>;
}

const ConversationContext = createContext<ConversationContextValue | null>(null);

async function responseJson<T>(response: Response): Promise<T> {
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.detail || body.error || 'Conversation request failed');
  }
  return body as T;
}

export function ConversationProvider({ children }: { children: React.ReactNode }) {
  const session = useSessionContext();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<PersistedMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reconnectInFlightRef = useRef<Promise<void> | null>(null);

  const loadDetail = useCallback(async (id: string) => {
    const detail = await responseJson<{ messages: PersistedMessage[] }>(
      await fetch(`/api/conversations/${encodeURIComponent(id)}`, { cache: 'no-store' })
    );
    setMessages(detail.messages);
  }, []);

  const refreshListing = useCallback(async () => {
    const listing = await responseJson<{
      active_id: string;
      conversations: ConversationSummary[];
    }>(await fetch('/api/conversations', { cache: 'no-store' }));
    setConversations(listing.conversations);
    setActiveId(listing.active_id);
    return listing.active_id;
  }, []);

  const refresh = useCallback(async () => {
    try {
      const selectedId = await refreshListing();
      await loadDetail(selectedId);
      setError(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Unable to load conversations'
      );
    } finally {
      setLoading(false);
    }
  }, [loadDetail, refreshListing]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const room = session.room;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    const handleData = (
      _payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string
    ) => {
      if (topic !== 'conversation_updated') return;
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => void refreshListing(), 150);
    };
    room.on(RoomEvent.DataReceived, handleData);
    return () => {
      if (refreshTimer) clearTimeout(refreshTimer);
      room.off(RoomEvent.DataReceived, handleData);
    };
  }, [refreshListing, session.room]);

  useEffect(() => {
    if (!session.isConnected && !loading) void refresh();
  }, [session.isConnected, loading, refresh]);

  const reconnectAround = useCallback(
    async (action: () => Promise<string>) => {
      if (reconnectInFlightRef.current) {
        await reconnectInFlightRef.current;
        return;
      }

      const operation = (async () => {
        const reconnect = session.isConnected;
        if (reconnect) {
          // Subscribe before ending the room. Awaiting end() alone can resume before
          // useSession's React state has observed the disconnect. Calling start() in
          // that gap makes its waitUntilDisconnected() miss the completed transition.
          const disconnected = session.waitUntilDisconnected();
          await session.end();
          await disconnected;

          // The connection endpoint deliberately reuses a fixed room name. Delete
          // the old room explicitly so the next token always creates a fresh room
          // and dispatch instead of racing LiveKit's departure timeout.
          await responseJson(
            await fetch('/api/session/reset', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
            })
          );
        }

        const selectedId = await action();
        setActiveId(selectedId);
        await refresh();
        if (reconnect) {
          await startSessionWithMicrophonePreference(session);
        }
      })();

      reconnectInFlightRef.current = operation;
      try {
        await operation;
      } finally {
        if (reconnectInFlightRef.current === operation) {
          reconnectInFlightRef.current = null;
        }
      }
    },
    [refresh, session]
  );

  const createConversation = useCallback(async () => {
    setError(null);
    try {
      await reconnectAround(async () => {
        const result = await responseJson<{ active_id: string }>(
          await fetch('/api/conversations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: 'New conversation' }),
          })
        );
        return result.active_id;
      });
      return true;
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Unable to create conversation'
      );
      return false;
    }
  }, [reconnectAround]);

  const selectConversation = useCallback(
    async (id: string) => {
      if (id === activeId) return;
      setError(null);
      try {
        await reconnectAround(async () => {
          const result = await responseJson<{ active_id: string }>(
            await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ active: true }),
            })
          );
          return result.active_id;
        });
      } catch (requestError) {
        setError(
          requestError instanceof Error ? requestError.message : 'Unable to select conversation'
        );
      }
    },
    [activeId, reconnectAround]
  );

  const deleteConversation = useCallback(
    async (id: string) => {
      setError(null);
      try {
        await reconnectAround(async () => {
          const result = await responseJson<{ active_id: string }>(
            await fetch(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' })
          );
          return result.active_id;
        });
        return true;
      } catch (requestError) {
        setError(
          requestError instanceof Error ? requestError.message : 'Unable to delete conversation'
        );
        return false;
      }
    },
    [reconnectAround]
  );

  const renameConversation = useCallback(async (id: string, title: string) => {
    const cleanedTitle = title.trim();
    if (!cleanedTitle) return false;

    setError(null);
    try {
      const result = await responseJson<{ title: string }>(
        await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: cleanedTitle }),
        })
      );
      setConversations((current) =>
        current.map((conversation) =>
          conversation.id === id ? { ...conversation, title: result.title } : conversation
        )
      );
      return true;
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Unable to rename conversation'
      );
      return false;
    }
  }, []);

  const value = useMemo(
    () => ({
      conversations,
      activeId,
      messages,
      loading,
      error,
      createConversation,
      selectConversation,
      renameConversation,
      deleteConversation,
      refresh,
    }),
    [
      conversations,
      activeId,
      messages,
      loading,
      error,
      createConversation,
      selectConversation,
      renameConversation,
      deleteConversation,
      refresh,
    ]
  );

  return <ConversationContext.Provider value={value}>{children}</ConversationContext.Provider>;
}

export function useConversations() {
  const context = useContext(ConversationContext);
  if (!context) throw new Error('useConversations must be used inside ConversationProvider');
  return context;
}
