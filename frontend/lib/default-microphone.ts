import type { Room } from 'livekit-client';

export async function preferSystemDefaultMicrophone(room: Room): Promise<boolean> {
  try {
    await room.switchActiveDevice('audioinput', 'default');
    console.info('[Audio] Following the macOS system-default microphone');
    return true;
  } catch (error) {
    console.warn('[Audio] Could not select the system-default microphone', error);
    return false;
  }
}
