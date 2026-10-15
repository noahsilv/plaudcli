import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from plaud_client import PlaudClient, PlaudError, TokenStore, render_transcript, validate_download_url, window


class MemoryStore:
    redis = None

    def __init__(self, tokens=None):
        self.tokens = tokens or {"access_token": "fake-access", "refresh_token": "fake-refresh"}
        self.lock = asyncio.Lock()

    def locked(self):
        return self.lock

    async def load(self):
        return dict(self.tokens)

    async def save(self, tokens):
        self.tokens = tokens


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_tokens_rotate_once_for_concurrent_requests(self):
        store = MemoryStore({"access_token": "old", "refresh_token": "old-refresh", "expires_at": 1})
        refreshes = []

        async def handler(request):
            if request.method == "POST":
                refreshes.append(request.content)
                await asyncio.sleep(0.01)
                return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
            self.assertEqual(request.headers["Authorization"], "Bearer new")
            return httpx.Response(200, json={"id": "meeting"})

        client = PlaudClient(store, httpx.MockTransport(handler))
        await asyncio.gather(client.get_file("meeting"), client.get_file("meeting"))
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(store.tokens["refresh_token"], "rotated")
        self.assertGreater(store.tokens["expires_at"], time.time() * 1000)

    async def test_401_refresh_retry_and_redacted_errors(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
            if request.headers.get("Authorization") == "Bearer fake-access":
                return httpx.Response(401, text="secret-token-never-expose")
            return httpx.Response(200, json={"id": "ok"})

        client = PlaudClient(MemoryStore(), httpx.MockTransport(handler))
        self.assertEqual((await client.get_file("ok"))["id"], "ok")
        self.assertEqual(len(calls), 3)
        client = PlaudClient(MemoryStore(), httpx.MockTransport(lambda r: httpx.Response(403, text="secret-token-never-expose")))
        with self.assertRaises(PlaudError) as caught:
            await client.get_file("ok")
        self.assertNotIn("secret-token", str(caught.exception))

    async def test_refresh_transport_failure_retains_credentials(self):
        store = MemoryStore({"access_token": "old", "refresh_token": "keep", "expires_at": 1})
        client = PlaudClient(store, httpx.MockTransport(lambda r: httpx.Response(503, text="secret")))
        with self.assertRaises(PlaudError):
            await client.get_file("meeting")
        self.assertEqual(store.tokens["refresh_token"], "keep")

    async def test_beijing_date_boundary_and_page_continuation(self):
        pages = []

        def handler(request):
            page = int(request.url.params["page"])
            pages.append(page)
            rows = ([{"id": str(i), "name": "Other", "created_at": "2026-10-14T15:59:00Z"} for i in range(9)] +
                    [{"id": "target", "name": "Budget Anzheng", "start_at": "2026-10-14T16:01:00Z"}]) if page == 1 else []
            return httpx.Response(200, json={"data": rows})

        client = PlaudClient(MemoryStore(), httpx.MockTransport(handler))
        result = await client.list_recordings(page_size=10, date_from="2026-10-15", date_to="2026-10-15", query="budget")
        self.assertEqual([r["id"] for r in result["recordings"]], ["target"])
        self.assertEqual(result["next_page"], 2)
        self.assertTrue(result["scan_limit_reached"])
        result = await client.list_recordings(page=2, page_size=10)
        self.assertIsNone(result["next_page"])
        self.assertEqual(pages, [1, 2])

    async def test_invalid_date_and_timezone(self):
        client = PlaudClient(MemoryStore())
        for kwargs in ({"date_from": "2026-02-30"}, {"timezone_name": "Bad/Zone"}, {"date_from": "2026-10-16", "date_to": "2026-10-15"}):
            with self.assertRaises(PlaudError):
                await client.list_recordings(**kwargs)

    async def test_signed_block_download_has_no_bearer_and_rejects_redirects(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="signed transcript")

        client = PlaudClient(MemoryStore(), httpx.MockTransport(handler))
        from unittest.mock import AsyncMock
        with patch("plaud_client.validate_download_url", new=AsyncMock()):
            text = await client.block_text({"data_link": "https://storage.example.test/signed"})
        self.assertEqual(text, "signed transcript")
        self.assertNotIn("Authorization", seen[0].headers)
        client = PlaudClient(MemoryStore(), httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": "https://elsewhere.test"})))
        with patch("plaud_client.validate_download_url", new=AsyncMock()):
            with self.assertRaises(PlaudError):
                await client.block_text({"data_link": "https://storage.example.test/signed"})

    async def test_private_download_targets_rejected(self):
        for url in ("http://example.test/a", "https://127.0.0.1/a", "https://user:pass@example.test/a", "https://example.test:8443/a"):
            with self.assertRaises(PlaudError):
                await validate_download_url(url)
        with patch("asyncio.BaseEventLoop.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.1", 443))]):
            with self.assertRaises(PlaudError):
                await validate_download_url("https://private.example.test/a")

    async def test_file_store_seed_does_not_replace_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = str(Path(directory) / "tokens.json")
            with patch.dict(os.environ, {"PLAUD_TOKEN_FILE": token_path, "PLAUD_TOKENS_JSON": '{"access_token":"seed"}', "PLAUD_REDIS_URL": ""}):
                store = TokenStore()
                self.assertEqual((await store.load())["access_token"], "seed")
                await store.save({"access_token": "rotated"})
                self.assertEqual((await TokenStore().load())["access_token"], "rotated")


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_tool_schemas_and_reading(self):
        import server
        from fastmcp import Client
        file = {"id": "test", "name": "Meeting", "presigned_url": "https://private-audio.test/signed",
                "source_list": [{"data_type": "transaction", "data_content": json.dumps([
                    {"speaker": "Anzheng", "start_time": 1000, "content": "Three phases."}])}],
                "note_list": [{"data_type": "auto_sum_note", "data_content": "Summary"},
                              {"data_type": "custom", "data_tab_name": "Decisions", "data_content": "Agreed"}]}
        mock_client = PlaudClient(MemoryStore(), httpx.MockTransport(lambda r: httpx.Response(200, json=file)))
        with patch.object(server, "client", mock_client):
            async with Client(server.mcp) as mcp_client:
                names = {t.name for t in await mcp_client.list_tools()}
                self.assertEqual(len(names), 7)
                result = await mcp_client.call_tool("get_meeting_package", {"recording_id": "test"})
                data = result.data
                self.assertIn("Anzheng", data["transcript"]["text"])
                self.assertEqual(len(data["notes"]), 2)
                self.assertNotIn("private-audio", json.dumps(data))
                result = await mcp_client.call_tool("get_notes", {"recording_id": "test", "note_index": 1})
                self.assertEqual(result.data["text"], "Agreed")
                result = await mcp_client.call_tool("get_transcript", {"recording_id": "test", "block": "transaction_polish"})
                self.assertEqual(result.data["status"], "unavailable")


class RenderingTests(unittest.TestCase):
    def test_pagination_is_lossless_for_chinese_and_timestamps(self):
        text = render_transcript(json.dumps([{"speaker": "??", "start_time": 1234,
                                              "end_time": 2345, "content": "??" * 150}]))
        pieces, offset = [], 0
        while True:
            chunk = window(text, offset, 100)
            pieces.append(chunk["text"])
            if chunk["next_offset"] is None:
                break
            offset = chunk["next_offset"]
        self.assertEqual("".join(pieces), text)
        self.assertIn("[1234 - 2345]", text)


if __name__ == "__main__":
    unittest.main()
