import type { ThemeName } from './lib/theme';

export interface AppConfig {
  pageTitle: string;
  pageDescription: string;
  companyName: string;

  supportsChatInput: boolean;
  supportsVideoInput: boolean;
  supportsScreenShare: boolean;
  isPreConnectBufferEnabled: boolean;

  logo: string;
  theme?: ThemeName; // Use theme system instead of individual accent colors

  // Legacy support - deprecated, use theme instead
  accent?: string;
  logoDark?: string;
  accentDark?: string;

  // for LiveKit Cloud Sandbox
  sandboxId?: string;
  agentName?: string;
}

export const APP_CONFIG_DEFAULTS: AppConfig = {
  companyName: 'CAAL',
  pageTitle: 'CAAL Voice Assistant',
  pageDescription: 'A local voice assistant with n8n workflow integrations',

  supportsChatInput: true,
  supportsVideoInput: false,
  supportsScreenShare: false,
  isPreConnectBufferEnabled: true,

  logo: '/cwl-logo-round.png',
  theme: 'midnight', // Use Midnight theme

  // for LiveKit Cloud Sandbox
  sandboxId: undefined,
  agentName: undefined,
};
