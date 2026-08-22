import { Room } from 'livekit-client';

const BUILT_IN_MICROPHONE_LABELS = [
  /macbook.*(?:microphone|\bmic\b|麥克風)/i,
  /(?:microphone|\bmic\b|麥克風).*macbook/i,
  /(?:built[ -]?in|internal|內建|內置).*(?:microphone|\bmic\b|麥克風)/i,
];

function isBuiltInMacMicrophone(device: MediaDeviceInfo) {
  if (device.kind !== 'audioinput' || ['default', 'communications'].includes(device.deviceId)) {
    return false;
  }
  return BUILT_IN_MICROPHONE_LABELS.some((pattern) => pattern.test(device.label));
}

export async function preferBuiltInMacMicrophone(room: Room): Promise<boolean> {
  try {
    const devices = await Room.getLocalDevices('audioinput', true);
    const builtInMicrophone = devices.find(isBuiltInMacMicrophone);
    if (!builtInMicrophone) {
      console.warn('[Audio] MacBook built-in microphone was not found; using browser default');
      return false;
    }

    await room.switchActiveDevice('audioinput', builtInMicrophone.deviceId);
    console.info(`[Audio] Using built-in microphone: ${builtInMicrophone.label}`);
    return true;
  } catch (error) {
    console.warn('[Audio] Could not select the MacBook built-in microphone', error);
    return false;
  }
}
