'use client';

import { useCallback, useState } from 'react';
import { useSessionContext } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { SessionView } from '@/components/app/session-view';
import { MemoryPanel } from '@/components/memory';
import { SettingsPanel } from '@/components/settings/settings-panel';
import { ToolsPanel } from '@/components/tools';
import { startSessionWithMicrophonePreference } from '@/lib/microphone-preference';

interface ViewControllerProps {
  appConfig: AppConfig;
}

export function ViewController({ appConfig }: ViewControllerProps) {
  const session = useSessionContext();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);

  const start = useCallback(async () => {
    await startSessionWithMicrophonePreference(session);
  }, [session]);

  return (
    <>
      <SessionView
        appConfig={appConfig}
        onStartCall={start}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenTools={() => setToolsOpen(true)}
        onOpenMemory={() => setMemoryOpen(true)}
      />

      {/* Settings panel */}
      <SettingsPanel isOpen={settingsOpen} onClose={() => setSettingsOpen(false)} />

      {/* Tools panel */}
      <ToolsPanel isOpen={toolsOpen} onClose={() => setToolsOpen(false)} />

      {/* Memory panel */}
      <MemoryPanel isOpen={memoryOpen} onClose={() => setMemoryOpen(false)} />
    </>
  );
}
