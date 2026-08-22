'use client';

import { useEffect, useState } from 'react';
import { Track } from 'livekit-client';
import { AudioTrack, useTracks } from '@livekit/components-react';
import { DEFAULT_VOICE_VOLUME, VOICE_VOLUME_EVENT, normalizeVoiceVolume } from '@/lib/voice-volume';

/**
 * Custom audio renderer that only plays audio from agent participants.
 * This prevents hearing other human participants in multi-device setups.
 */
export function AgentAudioRenderer() {
  const [volume, setVolume] = useState(DEFAULT_VOICE_VOLUME);
  const tracks = useTracks(
    [Track.Source.Microphone, Track.Source.ScreenShareAudio, Track.Source.Unknown],
    {
      updateOnlyOn: [],
      onlySubscribed: true,
    }
  ).filter(
    (ref) =>
      !ref.participant.isLocal &&
      ref.publication.kind === Track.Kind.Audio &&
      ref.participant.isAgent
  );

  useEffect(() => {
    let isActive = true;

    const loadVolume = async () => {
      try {
        const response = await fetch('/api/settings');
        if (!response.ok) return;

        const data = await response.json();
        if (isActive) {
          setVolume(normalizeVoiceVolume(data.settings?.voice_volume));
        }
      } catch (error) {
        console.warn('Unable to load voice volume; using the default.', error);
      }
    };

    const handleVolumeChange = (event: Event) => {
      setVolume(normalizeVoiceVolume((event as CustomEvent<number>).detail));
    };

    void loadVolume();
    window.addEventListener(VOICE_VOLUME_EVENT, handleVolumeChange);

    return () => {
      isActive = false;
      window.removeEventListener(VOICE_VOLUME_EVENT, handleVolumeChange);
    };
  }, []);

  return (
    <div style={{ display: 'none' }}>
      {tracks.map((trackRef) => (
        <AudioTrack
          key={`${trackRef.participant.identity}-${trackRef.publication.trackSid}`}
          trackRef={trackRef}
          volume={volume}
        />
      ))}
    </div>
  );
}
