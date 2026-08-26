export interface MirroredTranscriptMessage {
  id: string;
  content: string;
  role: 'user' | 'assistant' | 'tool';
  createdAt: number;
  partial: boolean;
  toolStatus?: 'running' | 'complete' | 'failed';
  toolParams?: Record<string, unknown>[];
}

export interface DesktopControlState {
  controlProtocolVersion: number;
  clientId: string | null;
  connected: boolean;
  agentReady: boolean;
  activeConversationId: string | null;
  activeConversationTitle: string | null;
  status: string;
  microphoneEnabled: boolean;
  completedControlCommandId: string | null;
  controlCommandError: string | null;
  liveMessages: MirroredTranscriptMessage[];
  updatedAt: number;
}

export interface MobileControlSnapshot {
  desktop: DesktopControlState;
  pendingConversationId: string | null;
  pendingCommandId: string | null;
  pendingControlCommand: DesktopControlCommand | null;
}

export interface ConversationSelectionCommand {
  commandId: string;
  conversationId: string;
  conversationTitle: string | null;
  createdAt: number;
}

export type DesktopControlAction =
  | 'start_call'
  | 'end_call'
  | 'set_microphone_enabled'
  | 'create_conversation';

export interface DesktopControlCommand {
  commandId: string;
  action: DesktopControlAction;
  microphoneEnabled?: boolean;
  createdAt: number;
}

type Listener = (event: string, data: unknown) => void;

class MobileControlStore {
  private desktop: DesktopControlState = {
    controlProtocolVersion: 1,
    clientId: null,
    connected: false,
    agentReady: false,
    activeConversationId: null,
    activeConversationTitle: null,
    status: 'offline',
    microphoneEnabled: false,
    completedControlCommandId: null,
    controlCommandError: null,
    liveMessages: [],
    updatedAt: 0,
  };

  private pendingConversationId: string | null = null;
  private pendingCommandId: string | null = null;
  private pendingControlCommand: DesktopControlCommand | null = null;
  private listeners = new Set<Listener>();

  snapshot(): MobileControlSnapshot {
    return {
      desktop: this.desktop,
      pendingConversationId: this.pendingConversationId,
      pendingCommandId: this.pendingCommandId,
      pendingControlCommand: this.pendingControlCommand,
    };
  }

  updateDesktop(state: DesktopControlState): MobileControlSnapshot {
    this.desktop = {
      ...state,
      liveMessages: state.liveMessages.slice(-50),
      updatedAt: Date.now(),
    };

    if (
      this.pendingConversationId &&
      state.agentReady &&
      state.activeConversationId === this.pendingConversationId
    ) {
      this.pendingConversationId = null;
      this.pendingCommandId = null;
    }

    if (
      this.pendingControlCommand &&
      state.completedControlCommandId === this.pendingControlCommand.commandId
    ) {
      this.pendingControlCommand = null;
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

  requestControl(action: DesktopControlAction, microphoneEnabled?: boolean): DesktopControlCommand {
    if (this.pendingControlCommand) {
      throw new Error('Another desktop control is still in progress');
    }
    if (action === 'set_microphone_enabled' && typeof microphoneEnabled !== 'boolean') {
      throw new Error('microphoneEnabled is required for microphone control');
    }

    const command: DesktopControlCommand = {
      commandId: crypto.randomUUID(),
      action,
      microphoneEnabled,
      createdAt: Date.now(),
    };
    this.pendingControlCommand = command;
    this.publish('control_command', command);
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
