'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Room, TokenSource } from 'livekit-client';
import { SessionProvider, StartAudio, useSession } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { AgentAudioRenderer } from '@/components/app/agent-audio-renderer';
import { ViewController } from '@/components/app/view-controller';
import { WakeWordProvider } from '@/components/app/wake-word-provider';
import { ConversationLayout } from '@/components/conversations/conversation-sidebar';
import { Toaster } from '@/components/livekit/toaster';
import { DesktopMobileBridge } from '@/components/mobile/desktop-mobile-bridge';
import { LanguageSelector, SetupWizard } from '@/components/setup';
import { useCaalTheme } from '@/hooks/useCaalTheme';
// import { useAgentErrors } from '@/hooks/useAgentErrors';
import { useConnectionErrors } from '@/hooks/useConnectionErrors';
import { ConversationProvider } from '@/hooks/useConversations';
import { useDebugMode } from '@/hooks/useDebug';
import { getSandboxTokenSource } from '@/lib/utils';

// Porcupine access key from environment
const PORCUPINE_ACCESS_KEY = process.env.NEXT_PUBLIC_PORCUPINE_ACCESS_KEY ?? '';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  // useAgentErrors(); // Disabled for multi-device support - timeout breaks second device
  useConnectionErrors(); // Show MCP connection errors from agent

  return null;
}

interface AppProps {
  appConfig: AppConfig;
}

export function App({ appConfig }: AppProps) {
  const [setupCompleted, setSetupCompleted] = useState<boolean | null>(null);
  const [localeChosen, setLocaleChosen] = useState<boolean | null>(null);

  // Initialize CAAL theme from saved settings
  useCaalTheme();

  // Check setup status and sync locale cookie on mount
  useEffect(() => {
    const init = async () => {
      const hasCookie = document.cookie.includes('CAAL_LOCALE');

      try {
        const res = await fetch('/api/setup/status');
        const data = await res.json();
        const completed = data.completed ?? false;

        // If setup done but no cookie (e.g. new browser), sync from backend settings
        if (completed && !hasCookie) {
          try {
            const settingsRes = await fetch('/api/settings');
            if (settingsRes.ok) {
              const settingsData = await settingsRes.json();
              const lang = settingsData.settings?.language || 'en';
              document.cookie = `CAAL_LOCALE=${lang};path=/;max-age=31536000;SameSite=Lax`;
              if (lang !== 'en') {
                // Reload to pick up non-English locale
                window.location.reload();
                return;
              }
            }
          } catch {
            // Best-effort — default to English
          }
          setLocaleChosen(true);
        } else {
          setLocaleChosen(hasCookie);
        }

        setSetupCompleted(completed);
      } catch {
        setSetupCompleted(false);
        setLocaleChosen(hasCookie);
      }
    };
    init();
  }, []);

  const handleSetupComplete = () => {
    setSetupCompleted(true);
    // Reload the page to pick up new settings
    window.location.reload();
  };

  const tokenSource = useMemo(() => {
    return typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === 'string'
      ? getSandboxTokenSource(appConfig)
      : TokenSource.endpoint('/api/connection-details');
  }, [appConfig]);

  const room = useMemo(() => new Room({ webAudioMix: true }), []);

  const session = useSession(
    tokenSource,
    appConfig.agentName ? { agentName: appConfig.agentName, room } : { room }
  );

  // Clean up session on page unload to prevent orphaned agent jobs
  useEffect(() => {
    const handleUnload = () => {
      session.end();
    };
    window.addEventListener('beforeunload', handleUnload);
    window.addEventListener('pagehide', handleUnload);
    return () => {
      window.removeEventListener('beforeunload', handleUnload);
      window.removeEventListener('pagehide', handleUnload);
    };
  }, [session]);

  // Handle wake word detection - unmute mic and call backend to trigger greeting
  const handleWakeWordDetected = useCallback(async () => {
    console.log('[App] Wake word detected');

    // Unmute microphone
    const micTrack = Array.from(
      session.room?.localParticipant?.audioTrackPublications.values() || []
    ).find((pub) => pub.source === 'microphone')?.track;

    if (micTrack && micTrack.isMuted) {
      console.log('[App] Unmuting microphone');
      await micTrack.unmute();
    }

    // Call backend to trigger greeting
    try {
      const response = await fetch('/api/wake', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room_name: 'voice_assistant_room' }),
      });
      if (!response.ok) {
        console.error('[App] Wake endpoint failed:', response.status);
      }
    } catch (error) {
      console.error('[App] Wake endpoint error:', error);
    }
  }, [session]);

  // Show loading state while checking setup status and locale
  if (setupCompleted === null || localeChosen === null) {
    return (
      <main className="grid h-svh grid-cols-1 place-content-center">
        <div className="text-muted-foreground text-center">Loading...</div>
      </main>
    );
  }

  // Show language selector before setup wizard (first-time users only)
  if (!setupCompleted && !localeChosen) {
    return <LanguageSelector onSelect={() => setLocaleChosen(true)} />;
  }

  // Show setup wizard if not completed
  if (!setupCompleted) {
    return <SetupWizard onComplete={handleSetupComplete} />;
  }

  return (
    <SessionProvider session={session}>
      <WakeWordProvider
        accessKey={PORCUPINE_ACCESS_KEY}
        keywordPath="/hey_cal.ppn"
        onWakeWordDetected={handleWakeWordDetected}
        defaultEnabled={false}
      >
        <AppSetup />
        <ConversationProvider>
          <DesktopMobileBridge />
          <ConversationLayout>
            <main className="h-full">
              <ViewController appConfig={appConfig} />
            </main>
          </ConversationLayout>
        </ConversationProvider>
        <StartAudio label="Start Audio" />
        <AgentAudioRenderer />
        <Toaster />
      </WakeWordProvider>
    </SessionProvider>
  );
}
