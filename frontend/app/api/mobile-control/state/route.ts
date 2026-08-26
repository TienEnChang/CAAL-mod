import { NextRequest, NextResponse } from 'next/server';
import { type DesktopControlState, mobileControlStore } from '@/lib/mobile-control-store';

export const dynamic = 'force-dynamic';

export async function GET() {
  return NextResponse.json(mobileControlStore.snapshot());
}

export async function POST(request: NextRequest) {
  const body = (await request.json()) as DesktopControlState;
  if (!body.clientId || typeof body.connected !== 'boolean') {
    return NextResponse.json({ error: 'Invalid desktop state' }, { status: 400 });
  }
  return NextResponse.json(mobileControlStore.updateDesktop(body));
}
