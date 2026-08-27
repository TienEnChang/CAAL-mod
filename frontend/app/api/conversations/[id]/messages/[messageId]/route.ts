import { NextRequest, NextResponse } from 'next/server';

const WEBHOOK_URL = process.env.WEBHOOK_URL || 'http://agent:8889';

interface RouteContext {
  params: Promise<{ id: string; messageId: string }>;
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  const { id, messageId } = await context.params;
  try {
    const response = await fetch(
      `${WEBHOOK_URL}/conversations/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}`,
      { method: 'DELETE' }
    );
    return NextResponse.json(await response.json(), { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unable to delete message' },
      { status: 500 }
    );
  }
}
