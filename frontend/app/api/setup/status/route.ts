import { NextResponse } from 'next/server';
import { readFile } from 'node:fs/promises';

const WEBHOOK_URL = process.env.WEBHOOK_URL || 'http://agent:8889';

async function localSetupStatus() {
  const settingsPath = process.env.CAAL_SETTINGS_PATH;
  if (!settingsPath) return null;

  try {
    const settings = JSON.parse(await readFile(settingsPath, 'utf8')) as {
      first_launch_completed?: boolean;
    };
    return typeof settings.first_launch_completed === 'boolean'
      ? settings.first_launch_completed
      : null;
  } catch (error) {
    console.error('[/api/setup/status] Could not read local settings:', error);
    return null;
  }
}

async function unavailableResponse() {
  const completed = await localSetupStatus();
  return NextResponse.json({ completed: completed ?? false });
}

export async function GET() {
  const localCompleted = await localSetupStatus();
  if (localCompleted === true) {
    return NextResponse.json({ completed: true });
  }

  try {
    const res = await fetch(`${WEBHOOK_URL}/setup/status`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!res.ok) {
      const text = await res.text();
      console.error('[/api/setup/status] Backend error:', res.status, text);
      return unavailableResponse();
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[/api/setup/status] Error:', error);
    return unavailableResponse();
  }
}
