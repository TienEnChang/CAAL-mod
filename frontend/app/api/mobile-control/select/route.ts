import { NextRequest, NextResponse } from 'next/server';
import { mobileControlStore } from '@/lib/mobile-control-store';

const WEBHOOK_URL = process.env.WEBHOOK_URL || 'http://agent:8889';

export async function POST(request: NextRequest) {
  const body = (await request.json()) as {
    conversationId?: string;
    conversationTitle?: string;
  };
  if (!body.conversationId) {
    return NextResponse.json({ error: 'conversationId is required' }, { status: 400 });
  }

  try {
    const conversation = await fetch(
      `${WEBHOOK_URL}/conversations/${encodeURIComponent(body.conversationId)}`,
      { cache: 'no-store' }
    );
    if (!conversation.ok) {
      return NextResponse.json({ error: 'Conversation not found' }, { status: 404 });
    }
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unable to reach CAAL agent' },
      { status: 503 }
    );
  }

  const command = mobileControlStore.requestConversation(
    body.conversationId,
    body.conversationTitle?.trim() || null
  );
  return NextResponse.json({ status: 'switching', ...command }, { status: 202 });
}
