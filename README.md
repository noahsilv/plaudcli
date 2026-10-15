# Plaud CLI Bridge for Horizon

A small, read-only FastMCP server for your own Plaud account. Deploy from this
repository to Prefect Horizon and connect compatible AI clients to one endpoint.
It provides structured results, explicit pagination and safe error messages.

## Design

This is an independent integration. It uses the **same consumer endpoints and
OAuth token format as the official `@plaud-ai/cli`**, verified against version
0.3.15. The hosted server is Python-only; it does not spawn the CLI or depend on
Node. Use the official CLI on your computer for initial browser login.

This avoids requiring Node and npm on Horizon, parsing colored terminal tables,
or exposing a generic shell tool. It still depends on Plaud's service and does
not provide offline access or protection against a Plaud-wide outage.

## Tools

| Tool | Purpose |
| --- | --- |
| `connection_status` | Check configuration; optionally verify upstream login |
| `list_recordings` | Title search and inclusive date filters, with scan pagination |
| `get_recording` | Metadata and inventory of transcript blocks and note tabs |
| `get_transcript` | Raw/polished transcript, outline or highlights; chunk pagination |
| `get_notes` | Any summary or custom note tab, with chunk pagination |
| `get_meeting_package` | Metadata, first raw transcript chunk and note inventory |
| `get_audio_url` | Temporary audio URL, only when specifically requested |

No editing, deletion, upload, external-model calls or recording actions.

## Horizon deployment

### 1. Authorize Plaud on your own computer

Install Node.js 20+ and run:

```powershell
npm install -g @plaud-ai/cli@0.3.15
plaud login
plaud files
```

The official CLI saves credentials to `~/.plaud/tokens.json`. Treat the entire
file as a password. Do not commit it, paste it into chat or add it to issues.

On Windows, copy it directly to the clipboard with the included helper:

```powershell
powershell -File scripts/copy_credentials.ps1
```

Paste the value into Horizon's **secret environment variable**
`PLAUD_TOKENS_JSON`. Clear your clipboard afterward. On another platform, copy
the file privately using your preferred secret-management workflow.

### 2. Configure durable credential storage

**Quick trial:** `PLAUD_TOKENS_JSON` is enough to seed a local token file on the
server. This file may disappear on a Horizon restart or redeployment. If Plaud
rotates the refresh token, the original environment value may no longer work.
Reauthorization and replacing the seed can then be necessary.

**Recommended for continued use on Horizon:** add `PLAUD_REDIS_URL` pointing to
your private Redis instance, using a TLS `rediss://` connection. Rotated tokens
are persisted there and a distributed lock coordinates concurrent refreshes.
Configure Redis for persistence and restrict access. Redis holds credentials,
not transcripts. The key defaults to `plaudcli:tokens`; change
`PLAUD_REDIS_KEY` if sharing a Redis instance between integrations.

The environment JSON seeds an empty store once. An existing Redis value always
wins. To replace a revoked login, stop the deployment, privately remove just
that token key, set a fresh `PLAUD_TOKENS_JSON` secret and redeploy. Never clear
the token key while requests are running. For long-term use, give the hosted
bridge its own authorization rather than continuing to share a rotating
credential set with an actively used local CLI.

### 3. Create the Horizon server

1. Select `noahsilv/plaudcli` in Horizon.
2. Set entrypoint to **`server.py:mcp`**.
3. Enable **Horizon authentication** and limit access to yourself.
4. Add `PLAUD_TOKENS_JSON` and, preferably, `PLAUD_REDIS_URL` as secrets.
5. Leave `PLAUD_TIMEZONE=Asia/Shanghai`, or set another IANA timezone.
6. Deploy. Horizon installs `requirements.txt` automatically.
7. In its Inspector, call `connection_status` with `check_login=true`, then
   `list_recordings` and `get_transcript` for a known recording.
8. Copy the generated MCP URL into each AI client's custom MCP settings and
   complete Horizon authorization.

Do not disable Horizon authentication: every authorized caller can read the
**same configured Plaud account**. This is a personal bridge, not a multi-user
service with a separate Plaud login for each caller. The code relies on Horizon
to enforce remote authentication. Local `python server.py` uses stdio.

## Search and reading behavior

- Title search is a case-insensitive substring match. It is not full-text
  transcript search.
- `list_recordings` scans 1-10 pages per call. Follow `next_page` until null.
  `scan_limit_reached` explicitly marks incomplete coverage. An exactly full
  final page may require one additional call to confirm the end.
- Date filters are applied locally to each returned recording, using
  `created_at_show`, then `start_at`, then `created_at`. Timestamps without a
  timezone are treated as UTC, matching the official CLI. Beijing calendar
  dates are used by default. Invalid/missing dates are counted explicitly.
- `get_transcript` defaults to the original `transaction` block. It never
  silently substitutes an AI-polished transcript or summary.
- Transcript rendering retains source speaker names and timing numbers without
  guessing units. Long responses return `next_offset`; fetch every chunk before
  claiming to have reviewed the whole meeting. Offsets count Python Unicode
  characters, so pass the returned value unchanged.
- `get_notes` exposes every tab through `note_index` and `total_notes`. A summary
  is AI-generated source material and should be verified against the transcript.
- `get_meeting_package` returns the first transcript chunk and a note inventory.
  Fetch the remaining chunks and notes using their dedicated tools.
- Source blocks may contain inline content or signed download links. The server
  resolves either form. Downloads require HTTPS, public DNS addresses, no
  redirects, and a 20 MB response limit. No Plaud bearer token is sent to signed
  download URLs. Optional `PLAUD_DATA_LINK_ALLOWED_HOSTS` restricts host suffixes.

Example prompt:

> Find today's discussion with Anzheng using Beijing dates. Retrieve the full
> raw transcript and all note tabs. Summarize the decisions and three proposed
> phases, citing speaker and source timing values. Identify uncertain statements.

## Configuration

| Variable | Default / purpose |
| --- | --- |
| `PLAUD_TOKENS_JSON` | Private JSON seed from the official CLI login |
| `PLAUD_REDIS_URL` | Optional private Redis URL for durable hosted credentials |
| `PLAUD_REDIS_KEY` | `plaudcli:tokens` |
| `PLAUD_TOKEN_FILE` | `~/.plaud/tokens.json`, when Redis is absent |
| `PLAUD_TIMEZONE` | `Asia/Shanghai` |
| `PLAUD_API_BASE` | `https://platform.plaud.ai/developer/api` |
| `PLAUD_REFRESH_URL` | API base + `/oauth/third-party/access-token/refresh` |
| `PLAUD_REGION` | Optional regional header, matching CLI |
| `PLAUD_DATA_LINK_ALLOWED_HOSTS` | Optional comma-separated content host suffixes |

Regional support depends on Plaud. This bridge does not make a China-region
account compatible with services unavailable to that account. Endpoint overrides
are trusted deployment settings; never accept them from tool callers.

## Local development

Python 3.11+ recommended.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\fastmcp inspect server.py:mcp
.venv\Scripts\python server.py
```

For local use, the server can read the official CLI's saved token file directly.
Avoid running the CLI and bridge concurrently against one local token file:
the file-mode refresh lock only coordinates requests within one server process.
Redis mode supports multiple bridge workers, but the official CLI does not use
the bridge's Redis lock.

Tests use fabricated credentials and mocked responses. They cover OAuth expiry
and refresh rotation, concurrent refreshes, error redaction, Beijing date
boundaries, pagination, download restrictions, note tabs and MCP tool execution.
They do not prove live account authorization. Run the Horizon Inspector checks
after providing credentials.

## Upstream references

- [Official Plaud CLI documentation](https://docs.plaud.ai/plaud-mcp-cli/cli)
- [Official CLI package](https://www.npmjs.com/package/@plaud-ai/cli)
- [Official Plaud MCP documentation](https://docs.plaud.ai/plaud-mcp-cli/mcp)
- [Horizon deployment guide](https://gofastmcp.com/deployment/prefect-horizon)

These consumer endpoints may change. If upstream behavior changes, compare
against the current official CLI before updating the integration. No Plaud
implementation code is vendored here.
