export interface MirroredTranscriptMessage {
  id: string;
  content: string;
  role: 'user' | 'assistant';
  createdAt: number;
  partial: boolean;
}

export interface DesktopControlState {
  clientId: string | null;
  connected: boolean;
  activeConversationId: string | null;
  activeConversationTitle: string | null;
  status: string;
  liveMessages: MirroredTranscriptMessage[];
  updatedAt: number;
}

export interface MobileControlSnapshot {
  desktop: DesktopControlState;
  pendingConversationId: string | null;
  pendingCommandId: string | null;
}

export interface ConversationSelectionCommand {
  commandId: string;
  conversationId: string;
  conversationTitle: string | null;
  createdAt: number;
}

type Listener = (event: string, data: unknown) => void;

class MobileControlStore {
  private desktop: DesktopControlState = {
    clientId: null,
    connected: false,
    activeConversationId: null,
    activeConversationTitle: null,
    status: 'offline',
    liveMessages: [],
    updatedAt: 0,
  };

  private pendingConversationId: string | null = null;
  private pendingCommandId: string | null = null;
  private listeners = new Set<Listener>();

  snapshot(): MobileControlSnapshot {
    return {
      desktop: this.desktop,
      pendingConversationId: this.pendingConversationId,
      pendingCommandId: this.pendingCommandId,
    };
  }

  updateDesktop(state: DesktopControlState): MobileControlSnapshot {
    this.desktop = {
      ...state,
      liveMessages: state.liveMessages.slice(-50),
      updatedAt: Date.now(),
    };

    if (this.pendingConversationId && state.activeConversationId === this.pendingConversationId) {
      this.pendingConversationId = null;
      this.pendingCommandId = null;
    }

    const snapshot = this.snapshot();
    this.publish('snapshot', snapshot);
    return snapshot;
  }

  requestConversation(
    conversationId: string,
    conversationTitle: string | null
  ): ConversationSelectionCommand {
    const command: ConversationSelectionCommand = {
      commandId: crypto.randomUUID(),
      conversationId,
      conversationTitle,
      createdAt: Date.now(),
    };
    this.pendingConversationId = conversationId;
    this.pendingCommandId = command.commandId;
    this.publish('select_conversation', command);
    this.publish('snapshot', this.snapshot());
    return command;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener('snapshot', this.snapshot());
    return () => this.listeners.delete(listener);
  }

  private publish(event: string, data: unknown): void {
    for (const listener of this.listeners) listener(event, data);
  }
}

declare global {
  var __caalMobileControlStore: MobileControlStore | undefined;
}

export const mobileControlStore =
  globalThis.__caalMobileControlStore ??
  (globalThis.__caalMobileControlStore = new MobileControlStore());
