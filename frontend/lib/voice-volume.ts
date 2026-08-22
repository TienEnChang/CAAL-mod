export const DEFAULT_VOICE_VOLUME = 1;
export const MAX_VOICE_VOLUME = 2;
export const VOICE_VOLUME_EVENT = 'caal:voice-volume-change';

export function normalizeVoiceVolume(value: unknown): number {
  const volume = typeof value === 'number' ? value : Number(value);

  if (!Number.isFinite(volume)) {
    return DEFAULT_VOICE_VOLUME;
  }

  return Math.min(MAX_VOICE_VOLUME, Math.max(0, volume));
}

export function announceVoiceVolume(value: unknown): void {
  if (typeof window === 'undefined') return;

  window.dispatchEvent(
    new CustomEvent<number>(VOICE_VOLUME_EVENT, {
      detail: normalizeVoiceVolume(value),
    })
  );
}
