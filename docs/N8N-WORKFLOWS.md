# CAAL n8n Workflow Integration

This guide explains how CAAL discovers and uses n8n workflows as voice-callable tools.

## Overview

CAAL transforms n8n workflows into LLM tools via the Model Context Protocol (MCP). Any n8n workflow with a webhook trigger and MCP enabled becomes a tool the voice assistant can call.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Voice Request                            │
│                    "What's the weather?"                        │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                          CAAL Agent                             │
│                                                                 │
│  1. LLM sees tool: weather_aus                                  │
│     Description: "Weather tool - forecast and current           │
│     conditions for Australia. Parameters: action..."            │
│                                                                 │
│  2. LLM decides to call tool with:                              │
│     {"action": "current", "location": "Sydney"}                 │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         n8n Workflow                             │
│                                                                 │
│  POST http://n8n:5678/webhook/weather_aus                       │
│  Body: {"action": "current", "location": "Sydney"}              │
│                                                                 │
│  Returns: {                                                     │
│    "message": "Sydney is 24 degrees and sunny",                 │
│    "weather": {"temp": 24, "condition": "sunny"}                │
│  }                                                              │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Voice Response                           │
│                  "Sydney is 24 degrees and sunny"               │
└─────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### 1. Discovery

When CAAL starts (or when `/reload-tools` is called), it discovers workflows via n8n's MCP server:

1. Calls `search_workflows` to get list of MCP-enabled workflows
2. Calls `get_workflow_details` for each to extract the webhook node's notes
3. Creates tool definitions with the workflow name and description

### 2. Tool Description

The **webhook node's `notes` field** becomes the tool description the LLM sees. This is how the LLM knows what the tool does and what parameters to pass.

Descriptions should start broad then scope, and clearly document parameters:

```
Weather tool - forecast and current conditions for Australia.

Parameters:
- action (required): 'forecast' or 'current'
- location (required): city, suburb, or postcode
- days (optional, default 7): forecast days ahead (1-7), use for 'next week' queries
- target_day (optional): specific weekday like 'Monday', use only for 'on Friday' style queries

Examples: 'Sydney weather', 'Melbourne next week' (days=7), 'Brisbane Thursday' (target_day='Thursday')
```

**Key principles:**
- **Start broad, then scope** - "Weather tool - forecast for Australia" not "Australian weather tool" (the latter won't match "Sydney weather" queries)
- **Use exact parameter names** - `query` not "search term", `days` not "number of days"
- **Disambiguate similar params** - explain when to use `days` vs `target_day`
- **Include examples** mapping natural language to parameters

The LLM reads this and knows to call the tool with `{"action": "current", "location": "Sydney"}`.

### 3. Execution

When the LLM decides to call a tool:

1. CAAL receives the tool call with arguments
2. CAAL POSTs to `{n8n_url}/webhook/{workflow_name}` with the arguments as JSON body
3. The n8n workflow executes and returns a response
4. CAAL speaks the `message` field to the user

### 4. Response Format

Workflows must return both a voice message and structured data:

```javascript
return {
  message: "Brief voice response here",  // Spoken aloud
  data: [...]  // Enables follow-up questions
};
```

**Why both?**
- `message` - Read aloud by the voice assistant
- `data` - Cached in conversation context for follow-up questions

Example:
```javascript
return {
  message: "Bills 20, Browns 10.",
  games: [
    { away: "BUF", awayScore: 20, home: "CLE", homeScore: 10, status: "live" },
    { away: "SEA", awayScore: 38, home: "LAR", homeScore: 37, status: "final" }
  ]
};
```

Now if the user asks "What about the Seahawks game?", the LLM can check the cached `games` array without re-calling the tool.

### 5. Naming Convention

**Tool Suites** (preferred for related actions) - a single workflow handling multiple actions via a Switch node:
- Name is just `service` (snake_case): `google_tasks`, `truenas`, `espn_nhl`
- Routes on an `action` parameter: `get`, `add`, `complete`, etc.

**Individual Tools** - single-purpose tools:
- Name follows `service_action_object`: `weather_get_forecast`, `date_calculate_days_until`

The workflow name becomes both the webhook path (`/webhook/google_tasks`) and the tool name the LLM sees.

---

## Voice Output Guidelines

All responses are read aloud. Follow these rules:

| Do | Don't |
|----|-------|
| Brief and conversational | Long detailed explanations |
| Plain text | Markdown, JSON, symbols |
| Natural language | Technical formats |
| Short names ("Falcons") | Full names ("Atlanta Falcons") |
| Limit lists (3-5 items) | Long enumerated lists |

**Good:** "Falcons 29, Bucs 28. Seahawks 16, Bears 7."

**Bad:** "Game 1: Atlanta Falcons versus Tampa Bay Buccaneers, final score 29 to 28..."

---

## Common Patterns

### Pattern 1: Simple API Query

```
Webhook → HTTP Request → Code (format) → Respond
```

### Pattern 2: Multi-Step Processing

```
Webhook → HTTP Request → Code (filter) → HTTP Request 2 → Code (format) → Respond
```

### Pattern 3: Async/Long-Running

For tasks taking >5 seconds, respond immediately then announce when done:

```
Webhook → Respond ("On it, I'll let you know")
    ↓
 [Long work]
    ↓
 HTTP Request → CAAL /announce endpoint
```

The announce call:
```json
{
  "method": "POST",
  "url": "http://caal-agent:8889/announce",
  "body": {"message": "Your task is complete."}
}
```

---

## n8n Setup

You can point CAAL at an n8n you already run, or let the native launcher host
one for you.

### Option A: Host n8n with the native runtime

`start-native.sh` can run n8n as a managed service alongside the other native
services. It installs into `.native/` and never touches a system-wide Node.

```bash
./start-native.sh --install-n8n
```

This downloads a dedicated Node runtime (n8n requires a newer Node than the
CAAL frontend uses) plus n8n itself, then generates an encryption key at
`.native/config/n8n-encryption-key`. Keep that file — it decrypts every
credential you store in n8n.

Once installed, n8n starts automatically with the rest of the stack:

```bash
./start-native.sh
```

| Detail | Value |
|--------|-------|
| Editor | `http://127.0.0.1:5678` |
| Data | `.native/data/n8n` |
| Log | `.native/logs/n8n.log` (`./start-native.sh --logs n8n`) |
| Port override | `CAAL_N8N_PORT` |
| Skip n8n | `CAAL_N8N_ENABLED=false` |

n8n binds to `127.0.0.1` only. Put it behind a reverse proxy with TLS and
authentication before exposing it to a network.

### Option B: Use an existing n8n

Leave n8n uninstalled locally (`CAAL_N8N_ENABLED=false`) and set `N8N_MCP_URL`
in `.env` to the remote instance's MCP endpoint.

### Enable the MCP server

This step happens in the n8n UI and cannot be automated:

1. Open n8n **Settings > Instance-level MCP**
2. Select **Enable MCP access** (requires owner or admin)
3. Under **Connection details**, open the access token and copy it — later
   visits only show a redacted value
4. Under **Workflows exposed**, enable the workflows CAAL should see

Only **published** workflows containing a webhook, form, schedule, or chat
trigger are eligible. Instance-level MCP needs n8n 2.33.0 or newer; the
launcher installs 2.36.7.

### Configure CAAL

Put the token in `.env`:

```
N8N_MCP_TOKEN=<token from n8n>
```

When the native launcher hosts n8n it exports `N8N_MCP_URL` for you, so leave
that blank. For a remote instance, set both.

Alternatively use CAAL's Settings Panel > Integrations > n8n and enter the host
URL and token there — settings values take priority over the environment, so
set both fields together or neither.

Restart the agent to pick up the change:

```bash
./start-native.sh --restart agent
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Tool not appearing | Check workflow is active and "Available in MCP" is enabled |
| Wrong parameters | Update webhook node notes with clear parameter docs |
| Empty response | Ensure Code node returns `{message: "...", data: ...}` |
| Webhook 404 | Verify workflow name matches webhook path exactly |
| Timeout | Use async pattern for long-running tasks |
| No tools discovered | Confirm the token is set and the workflow is exposed under Settings > Instance-level MCP |
| n8n won't start | Check `.native/logs/n8n.log`; port 5678 may already be in use (`CAAL_N8N_PORT`) |
| Credentials fail after reinstall | `.native/config/n8n-encryption-key` must match the one that encrypted them |

### Reload Tools

After creating or modifying workflows, reload CAAL's tool cache:

```bash
curl -X POST http://localhost:8889/reload-tools
```

Or say "reload tools" to the voice assistant.

---

## Creating & Sharing Tools

Browse community tools or submit your own via the **[CAAL Tool Registry](https://github.com/CoreWorxLab/caal-tools)**. Tools can be installed directly from the Tools panel in the CAAL web UI.

See the [CONTRIBUTING guide](https://github.com/CoreWorxLab/caal-tools/blob/main/CONTRIBUTING.md) for how to build and submit tools.
