import { NextRequest, NextResponse } from 'next/server';
import { RoomServiceClient } from 'livekit-server-sdk';

const ROOM_NAME = 'voice_assistant_room';

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
    const activeRooms = await rooms.listRooms([ROOM_NAME]);
    const deleted = activeRooms.length > 0;
    if (deleted) {
      await rooms.deleteRoom(ROOM_NAME);
    }
    return NextResponse.json({
      reset: true,
      deleted,
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Unable to reset the voice room' },
      { status: 500 }
    );
  }
}
