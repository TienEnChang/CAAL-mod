import type { Metadata } from 'next';
import { MobileTranscript } from '@/components/mobile/mobile-transcript';

export const metadata: Metadata = {
  title: 'CAAL Mobile Transcript',
  description: 'Read-only live transcript and desktop conversation control for CAAL.',
};

export default function MobilePage() {
  return <MobileTranscript />;
}
