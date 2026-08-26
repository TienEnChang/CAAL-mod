import type { UseSessionReturn } from '@livekit/components-react';
import { preferSystemDefaultMicrophone } from '@/lib/default-microphone';

const MICROPHONE_ENABLED_KEY = 'caal-microphone-enabled';

export function getMicrophoneEnabledPreference(): boolean {
  if (typeof window === 'undefined') return true;
  return window.localStorage.getItem(MICROPHONE_ENABLED_KEY) !== 'false';
}

export function saveMicrophoneEnabledPreference(enabled: boolean): void {
  window.localStorage.setItem(MICROPHONE_ENABLED_KEY, String(enabled));
}

export async function startSessionWithMicrophonePreference(
  session: UseSessionReturn
): Promise<void> {
  const microphoneEnabled = getMicrophoneEnabledPreference();
  if (microphoneEnabled) {
    await preferSystemDefaultMicrophone(session.room);
  }
  await session.start({
    tracks: {
      microphone: {
        enabled: microphoneEnabled,
        publishOptions: { preConnectBuffer: true },
      },
    },
  });
}
