# Home Assistant Integration (Deprecated)

> Home Assistant support is deprecated and disabled. CAAL no longer loads the
> `home_assistant` MCP server or exposes the `hass` tool. Legacy configuration
> fields remain readable so existing backups can still be restored. The details
> below are retained only as historical implementation notes.

## Historical architecture

CAAL connects to Home Assistant's MCP server but exposes a single unified tool to the LLM:

| Tool | Purpose |
|------|---------|
| `hass(action, target, value)` | Control devices and check status |

This simplification (from 15+ raw MCP tools to 1 tool) dramatically improves LLM tool-calling reliability.

### Automatic Prefix Detection

Different Home Assistant MCP implementations use different tool naming conventions:
- Official HA MCP: bare names like `HassTurnOn`
- Some community servers: prefixed names like `assist__HassTurnOn`

CAAL automatically detects which prefix your server uses at startup, so you don't need to configure anything.

### Domain-Aware Intent Mapping

CAAL caches device information from Home Assistant to provide intelligent intent mapping. For example:
- "open the garage door" → Uses `HassOpenCover` (not `HassTurnOn`) because it's a cover device
- "turn on the office lamp" → Uses `HassTurnOn` for lights/switches
- "set thermostat to 72" → Uses `HassClimateSetTemperature` for climate devices

This domain-aware approach significantly improves reliability for devices like garage doors, blinds, and thermostats.

## hass

Control Home Assistant devices or get their status with a single action-based interface.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `action` | string | Yes | The action to perform (see table below) |
| `target` | string | No | Device name (e.g., "office lamp", "garage door"). Optional for `status`. |
| `value` | integer | No | Value for `set_volume`/`set_brightness` (0-100) or `set_temperature` (degrees) |

### Supported Actions

| Action | HASS MCP Tool | Description |
|--------|---------------|-------------|
| `status` | GetLiveContext | Get device state (target optional — omit for all devices) |
| `turn_on` | HassTurnOn / HassOpenCover* | Turn on a device/switch (or open a cover) |
| `turn_off` | HassTurnOff / HassCloseCover* | Turn off a device/switch (or close a cover) |
| `open` | HassOpenCover | Open a cover (garage door, blinds, etc.) |
| `close` | HassCloseCover | Close a cover |
| `stop` | HassStopMoving | Stop a cover mid-motion |
| `toggle` | HassToggle | Toggle device state |
| `set_brightness` | HassLightSet | Set light brightness (requires `value` 0-100) |
| `set_temperature` | HassClimateSetTemperature | Set thermostat temperature (requires `value`) |
| `pause` | HassMediaPause | Pause media playback |
| `play` | HassMediaUnpause | Resume media playback |
| `next` | HassMediaNext | Skip to next track |
| `previous` | HassMediaPrevious | Go to previous track |
| `volume_up` | HassSetVolumeRelative | Increase volume |
| `volume_down` | HassSetVolumeRelative | Decrease volume |
| `set_volume` | HassSetVolume | Set volume to specific level (requires `value` 0-100) |
| `mute` | HassMediaPlayerMute | Mute audio |
| `unmute` | HassMediaPlayerUnmute | Unmute audio |

*Domain-aware: `turn_on`/`turn_off` automatically use cover intents for cover devices.

### Examples

```
"Turn on the office lamp"
→ hass(action="turn_on", target="office lamp")

"Open the garage door"
→ hass(action="open", target="garage door")

"Close the blinds"
→ hass(action="close", target="blinds")

"Set the thermostat to 72"
→ hass(action="set_temperature", target="thermostat", value=72)

"Set bedroom lights to 50 percent"
→ hass(action="set_brightness", target="bedroom lights", value=50)

"Pause the Apple TV"
→ hass(action="pause", target="apple tv")

"Set the soundbar volume to 30"
→ hass(action="set_volume", target="soundbar", value=30)

"What's the status of the garage door?"
→ hass(action="status", target="garage door")

"What devices are on?"
→ hass(action="status")
```

## Prompt Configuration

The default prompt (`prompt/default.md`) includes instructions for using the hass tool:

```markdown
# Home Control (hass)

Control devices or check status with: `hass(action, target, value)`
- **action**: status, turn_on, turn_off, open, close, toggle, volume_up, volume_down, set_volume, mute, unmute, pause, play, next, previous, set_brightness, set_temperature, stop
- **target**: Device name like "office lamp", "garage door", or "thermostat" (optional for status)
- **value**: For set_volume/set_brightness (0-100), set_temperature (degrees)

Examples:
- "turn on the office lamp" → `hass(action="turn_on", target="office lamp")`
- "open the garage door" → `hass(action="open", target="garage door")`
- "set thermostat to 72" → `hass(action="set_temperature", target="thermostat", value=72)
- "is the garage door open?" → `hass(action="status", target="garage door")`

Act immediately - don't ask for confirmation. Confirm AFTER the action completes.
```

## Advanced: Raw MCP Tools

For power users who need full access to all 15 HASS MCP tools:

1. Add Home Assistant manually via `mcp_servers.json`:

```json
{
  "servers": [
    {
      "name": "hass_raw",
      "url": "http://homeassistant.local:8123/api/mcp",
      "token": "your-long-lived-token",
      "transport": "streamable_http"
    }
  ]
}
```

2. Create a custom prompt (`prompt/custom.md`) with instructions for the full tool set

Note: The wrapper tool will still be available alongside raw tools when using wizard-configured HASS.

## Troubleshooting

### "Home Assistant is not connected"

- Check that HASS URL is reachable from the CAAL container
- Verify the Long-Lived Access Token is valid
- Check HASS logs for MCP connection errors

### Device not found

- Device names must match exactly as shown in Home Assistant
- Use `hass(action="status")` to see available devices and their names
- Names are case-insensitive

### Action not working

- Ensure the device supports the action (e.g., lights don't support `pause`)
- Check Home Assistant for device-specific requirements
