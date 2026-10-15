"""Read-only Plaud client compatible with official CLI 0.3.15 credentials.

Consumer API paths and refresh format verified against @plaud-ai/cli 0.3.15.
This is an independent integration, not an official Plaud SDK.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx


class PlaudError(Exception):
    """Safe error message that never includes tokens, response bodies or URLs."""


def checked_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PlaudError("Configured endpoint must be an HTTPS URL without credentials.")
    return value.rstrip("/")


class TokenStore:
    """Redis is durable across Horizon deployments; files work on local machines.

    Environment JSON seeds an empty store only. Refreshes must never be lost by
    reloading the original environment value over newer stored credentials.
    """

    def __init__(self):
        if configured_path := os.environ.get("PLAUD_TOKEN_FILE"):
            self.path = Path(configured_path)
        else:
            try:
                self.path = Path.home() / ".plaud" / "tokens.json"
            except RuntimeError:
                self.path = Path(tempfile.gettempdir()) / "plaudcli" / "tokens.json"
        self.seed = os.environ.get("PLAUD_TOKENS_JSON", "")
        self.key = os.environ.get("PLAUD_REDIS_KEY", "plaudcli:tokens")
        self.redis = None
        self.local_lock = asyncio.Lock()
        if url := os.environ.get("PLAUD_REDIS_URL"):
            from redis.asyncio import Redis
            self.redis = Redis.from_url(url, decode_responses=True, socket_timeout=10)

    @asynccontextmanager
    async def locked(self):
        async with self.local_lock:
            if self.redis is not None:
                try:
                    async with self.redis.lock(self.key + ":lock", timeout=90, blocking_timeout=40):
                        yield
                except PlaudError:
                    raise
                except Exception:
                    raise PlaudError("Credential store unavailable. Check private Redis configuration.") from None
            else:
                yield

    async def load(self) -> dict:
        try:
            raw = await self.redis.get(self.key) if self.redis is not None else (
                self.path.read_text(encoding="utf-8") if self.path.exists() else None
            )
            if raw is None and self.seed:
                tokens = json.loads(self.seed)
                await self.save(tokens)
                return tokens
            return json.loads(raw) if raw else {}
        except PlaudError:
            raise
        except Exception:
            raise PlaudError("Cannot read Plaud credentials. Check token JSON and storage configuration.") from None

    async def save(self, tokens: dict):
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise PlaudError("Invalid credential payload: access_token is required.")
        raw = json.dumps(tokens)
        try:
            if self.redis is not None:
                await self.redis.set(self.key, raw)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".plaud-")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(raw)
                    os.chmod(name, 0o600)
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
        except Exception:
            raise PlaudError("Cannot persist refreshed credentials. Check credential storage.") from None


class PlaudClient:
    def __init__(self, store=None, transport=None):
        self.store = store or TokenStore()
        self.transport = transport
        self.api_base = checked_url(os.environ.get("PLAUD_API_BASE", "https://platform.plaud.ai/developer/api"))
        self.refresh_url = checked_url(os.environ.get("PLAUD_REFRESH_URL", self.api_base + "/oauth/third-party/access-token/refresh"))
        self.extra_headers = {"x-pld-region": os.environ["PLAUD_REGION"]} if os.environ.get("PLAUD_REGION") else {}

    def http(self):
        return httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport, trust_env=False)

    async def access_token(self, rejected_token: str | None = None) -> str:
        async with self.store.locked():
            tokens = await self.store.load()
            if not isinstance(tokens, dict) or not tokens.get("access_token"):
                raise PlaudError("Plaud login required. Run plaud login locally and configure private credentials.")
            token = tokens["access_token"]
            try:
                expires = float(tokens.get("expires_at") or 0)
            except (ValueError, TypeError):
                raise PlaudError("Invalid expires_at in Plaud credentials; expected epoch milliseconds.") from None
            expired = bool(expires and time.time() * 1000 >= expires - 60000)
            # A concurrent request may already have replaced the rejected token.
            if not expired and (rejected_token is None or token != rejected_token):
                return token
            if not tokens.get("refresh_token"):
                raise PlaudError("Plaud credentials expired and have no refresh token. Reauthorize locally.")
            try:
                async with self.http() as http:
                    response = await http.post(self.refresh_url, data={"refresh_token": tokens["refresh_token"]}, headers=self.extra_headers)
                if response.status_code in (400, 401, 403):
                    raise PlaudError("Plaud refresh rejected. Reauthorize locally and replace stored credentials.")
                response.raise_for_status()
                data = response.json()
                updated = {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token") or tokens["refresh_token"],
                    "token_type": data.get("token_type", "Bearer"),
                }
                if data.get("expires_in"):
                    updated["expires_at"] = time.time() * 1000 + float(data["expires_in"]) * 1000
                await self.store.save(updated)
                return updated["access_token"]
            except PlaudError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise PlaudError("Plaud token refresh failed. Credentials were retained; retry later.") from None

    async def request(self, path: str, params: dict | None = None):
        token = await self.access_token()
        try:
            async with self.http() as http:
                for attempt in range(2):
                    response = await http.get(self.api_base + path, params=params,
                                              headers={"Authorization": "Bearer " + token, "Accept": "application/json", **self.extra_headers})
                    if response.status_code == 401 and attempt == 0:
                        token = await self.access_token(rejected_token=token)
                        continue
                    if response.status_code >= 400:
                        messages = {401: "Plaud authorization failed.", 403: "Plaud permission denied.",
                                    404: "Plaud recording not found.", 429: "Plaud rate limit reached. Retry later."}
                        raise PlaudError(messages.get(response.status_code, "Plaud request failed. Retry later."))
                    response.raise_for_status()
                    return response.json()
        except PlaudError:
            raise
        except (httpx.HTTPError, ValueError):
            raise PlaudError("Plaud network or response error. Retry later.") from None

    async def get_file(self, recording_id: str) -> dict:
        if not recording_id or len(recording_id) > 256:
            raise PlaudError("Provide a recording ID from list_recordings.")
        result = await self.request("/open/third-party/files/" + quote(recording_id, safe=""))
        if not isinstance(result, dict):
            raise PlaudError("Unexpected recording response from Plaud.")
        return result

    async def list_recordings(self, page=1, page_size=50, query="", date_from=None, date_to=None, scan_pages=1, timezone_name="Asia/Shanghai"):
        if not 1 <= page <= 1000 or not 10 <= page_size <= 100 or not 1 <= scan_pages <= 10:
            raise PlaudError("Use page 1-1000, page_size 10-100 and scan_pages 1-10.")
        try:
            zone = ZoneInfo(timezone_name)
            start = date.fromisoformat(date_from) if date_from else None
            end = date.fromisoformat(date_to) if date_to else None
            if start and end and start > end:
                raise ValueError()
        except (ValueError, KeyError):
            raise PlaudError("Use YYYY-MM-DD dates, a valid timezone, and date_from <= date_to.") from None
        matches, scanned, unknown_dates = [], 0, 0
        more = False
        for current in range(page, min(page + scan_pages, 1001)):
            result = await self.request("/open/third-party/files/", {"page": current, "page_size": page_size})
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise PlaudError("Unexpected recording list response from Plaud.")
            scanned += len(rows)
            for row in rows:
                if query.casefold() not in str(row.get("name") or "").casefold():
                    continue
                if start or end:
                    day = recording_day(row, zone)
                    if day is None:
                        unknown_dates += 1
                        continue
                    if (start and day < start) or (end and day > end):
                        continue
                matches.append(metadata(row))
            more = len(rows) == page_size
            if not more:
                break
        return {"recordings": matches, "scanned": scanned, "timezone": timezone_name,
                "date_field_priority": ["created_at_show", "start_at", "created_at"],
                "skipped_unknown_dates": unknown_dates, "has_more": more,
                "next_page": current + 1 if more and current < 1000 else None,
                "scan_limit_reached": more, "search_scope": "recording titles only; scanned pages only"}

    async def block_text(self, block: dict) -> str:
        inline = block.get("data_content")
        if isinstance(inline, str) and inline:
            return inline
        if isinstance(inline, (dict, list)):
            return json.dumps(inline, ensure_ascii=False)
        link = block.get("data_link")
        if not link:
            return ""
        await validate_download_url(link)
        try:
            async with self.http() as http:
                async with http.stream("GET", link) as response:
                    response.raise_for_status()
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > 20 * 1024 * 1024:
                            raise PlaudError("Transcript or note exceeds the 20 MB download limit.")
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8-sig")
        except PlaudError:
            raise
        except (httpx.HTTPError, UnicodeError):
            raise PlaudError("Cannot download transcript or note content. Retry later.") from None


async def validate_download_url(link: str):
    try:
        parsed = urlsplit(link)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        suffixes = [s.strip().lower() for s in os.environ.get("PLAUD_DATA_LINK_ALLOWED_HOSTS", "").split(",") if s.strip()]
        if suffixes and not any(host.lower() == s or host.lower().endswith("." + s) for s in suffixes):
            raise ValueError()
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise PlaudError("Plaud content link rejected by download security checks.") from None


def recording_day(row: dict, zone):
    for field in ("created_at_show", "start_at", "created_at"):
        value = row.get(field)
        if not value:
            continue
        try:
            if isinstance(value, (int, float)):
                moment = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
            else:
                moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(zone).date()
        except (ValueError, OverflowError, OSError):
            continue
    return None


def metadata(file: dict) -> dict:
    return {key: file[key] for key in ("id", "name", "created_at", "created_at_show", "start_at", "duration") if key in file}


def window(text: str, offset=0, max_chars=20000):
    if offset < 0 or not 100 <= max_chars <= 100000:
        raise PlaudError("Use offset >= 0 and max_chars between 100 and 100000.")
    end = min(offset + max_chars, len(text))
    return {"text": text[offset:end], "offset": offset, "total_chars": len(text),
            "truncated": end < len(text), "next_offset": end if end < len(text) else None}


def render_transcript(content: str) -> str:
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return content
    if not isinstance(parsed, list):
        return content
    lines = []
    for segment in parsed:
        if not isinstance(segment, dict):
            lines.append(str(segment))
            continue
        # Preserve original timing values and units rather than guessing units.
        stamp = f"[{segment['start_time']}" if "start_time" in segment else ""
        if stamp:
            stamp += f" - {segment['end_time']}] " if "end_time" in segment else "] "
        speaker = str(segment.get("speaker") or "")
        lines.append(stamp + (speaker + ": " if speaker else "") + str(segment.get("content", segment.get("topic", ""))))
    return "\n".join(lines)
