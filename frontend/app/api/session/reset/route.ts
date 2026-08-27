import { NextRequest, NextResponse } from 'next/server';
import { RoomServiceClient } from 'livekit-server-sdk';

const ROOM_NAME = 'voice_assistant_room';
const DRAIN_TIMEOUT_MS = Number(process.env.CAAL_SESSION_DRAIN_TIMEOUT_MS ?? 35000);
const DRAIN_POLL_MS = 250;

export async function POST(request: NextRequest) {
  const requestHost = request.nextUrl.hostname;
  if (requestHost !== 'localhost' && requestHost !== '127.0.0.1' && requestHost !== '[::1]') {
    return NextResponse.json({ error: 'Session reset is only available locally' }, { status: 403 });
  }

  const livekitUrl = process.env.LIVEKIT_URL;
  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;
  if (!livekitUrl || !apiKey || !apiSecret) {
    return NextResponse.json(
      { error: 'LiveKit server credentials are unavailable' },
      { status: 500 }
    );
  }

  try {
    const rooms = new RoomServiceClient(livekitUrl, apiKey, apiSecret);
    let activeRooms = await rooms.listRooms([ROOM_NAME]);
    const hadActiveRoom = activeRooms.length > 0;

    // The agent performs bounded post-clearance summary and prefix warm-up after
    // the caller leaves. Let that job drain before deleting the fixed room;
    // otherwise a conversation switch can kill maintenance mid-transaction.
    const deadline = Date.now() + Math.max(0, DRAIN_TIMEOUT_MS);
    while (
      activeRooms.length > 0 &&
      (activeRooms[0].numParticipants ?? 0) > 0 &&
      Date.now() < deadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, DRAIN_POLL_MS));
      activeRooms = await rooms.listRooms([ROOM_NAME]);
    }

    const deleted = activeRooms.length > 0;
    if (deleted) {
      await rooms.deleteRoom(ROOM_NAME);
    }
    return NextResponse.json({
      reset: true,
      deleted,
      drained: hadActiveRoom && !deleted,
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unable to reset the voice room' },
      { status: 500 }
    );
  }
}
