import { NextRequest, NextResponse } from 'next/server';
import { type DesktopControlAction, mobileControlStore } from '@/lib/mobile-control-store';

const ACTIONS = new Set<DesktopControlAction>([
  'start_call',
  'end_call',
  'set_microphone_enabled',
  'create_conversation',
  'rename_conversation',
  'delete_conversation',
  'delete_message',
]);

export async function POST(request: NextRequest) {
  const body = (await request.json()) as {
    action?: DesktopControlAction;
    microphoneEnabled?: boolean;
    conversationId?: string;
    conversationTitle?: string;
    messageId?: string;
  };
  if (!body.action || !ACTIONS.has(body.action)) {
    return NextResponse.json({ error: 'Unsupported desktop control action' }, { status: 400 });
  }

  try {
    const command = mobileControlStore.requestControl(
      body.action,
      body.microphoneEnabled,
      body.conversationId,
      body.conversationTitle,
      body.messageId
    );
    return NextResponse.json({ status: 'pending', ...command }, { status: 202 });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unable to control the desktop' },
      { status: 409 }
    );
  }
}
