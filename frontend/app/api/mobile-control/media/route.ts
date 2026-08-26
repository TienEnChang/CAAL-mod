import { NextRequest, NextResponse } from 'next/server';
import { execFile } from 'node:child_process';
import { join, resolve } from 'node:path';
import { promisify } from 'node:util';

export const runtime = 'nodejs';

const execFileAsync = promisify(execFile);
const PROJECT_DIR = process.env.CAAL_PROJECT_DIR || resolve(process.cwd(), '../../..');
const MEDIA_CONTROL_SCRIPT = join(PROJECT_DIR, 'scripts', 'macos_media_control.js');
let toggleInFlight: Promise<{ ok: boolean }> | null = null;

async function toggleSystemMedia(): Promise<{ ok: boolean }> {
  if (toggleInFlight) return toggleInFlight;

  const operation = (async () => {
    const { stdout } = await execFileAsync(
      '/usr/bin/osascript',
      ['-l', 'JavaScript', MEDIA_CONTROL_SCRIPT, 'toggle'],
      { timeout: 5_000, maxBuffer: 16_384 }
    );
    return JSON.parse(stdout.trim()) as { ok: boolean };
  })();
  toggleInFlight = operation;

  try {
    return await operation;
  } finally {
    if (toggleInFlight === operation) toggleInFlight = null;
  }
}

export async function POST(request: NextRequest) {
  const body = (await request.json().catch(() => ({}))) as { action?: string };
  if (body.action !== 'toggle') {
    return NextResponse.json({ error: 'Media action must be toggle' }, { status: 400 });
  }

  try {
    return NextResponse.json(await toggleSystemMedia());
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unable to control system media' },
      { status: 500 }
    );
  }
}
