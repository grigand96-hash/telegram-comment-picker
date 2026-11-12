import os
import tempfile
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TEST_DATA = tempfile.TemporaryDirectory()
os.environ["ZNWR_PICKER_DATA_DIR"] = _TEST_DATA.name

from fastapi import HTTPException
from fastapi.testclient import TestClient
from telethon import types
import app


class PickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_is_readable_before_collection_finishes(self):
        first_page = asyncio.Event()
        resume = asyncio.Event()
        async def messages(*args, **kwargs):
            yield types.Message(id=1, peer_id=types.PeerChannel(1), message="First")
            first_page.set()
            await resume.wait()
            yield types.Message(id=2, peer_id=types.PeerChannel(1), message="Second")
        app.progress.update(request_id="test", phase="collecting", count=0)
        loading = asyncio.create_task(app.load_comments(SimpleNamespace(iter_messages=messages), "example", 1))
        await first_page.wait()
        self.assertEqual(await app.draw_progress("test"), {"phase": "collecting", "count": 1})
        self.assertEqual(await app.draw_progress("different-request"), {"phase": "waiting", "count": 0})
        resume.set()
        self.assertEqual(len(await loading), 2)

    def test_post_urls(self):
        self.assertEqual(app.parse_post("https://t.me/znwr_home/7079?single"), ("znwr_home", 7079, "https://t.me/znwr_home/7079"))
        for url in ["https://evil.example/znwr_home/7079", "https://t.me@evil.example/a/1", "https://t.me/znwr_home", "http://t.me/a/1"]:
            with self.assertRaises(HTTPException):
                app.parse_post(url)

    async def test_complete_thread_keeps_photo_and_distinct_comments(self):
        async def messages(*args, **kwargs):
            yield types.Message(id=1, peer_id=types.PeerChannel(1), message="First")
            yield types.MessageService(id=2, peer_id=types.PeerChannel(1), date=None, action=types.MessageActionEmpty())
            yield types.Message(id=3, peer_id=types.PeerChannel(1), message="")
        telegram = SimpleNamespace(iter_messages=messages)
        loaded = await app.load_comments(telegram, "channel", 1)
        self.assertEqual([message.id for message in loaded], [1, 3])

    async def test_repeat_draw_reuses_complete_thread(self):
        app.cached_url = None
        app.comments = []
        message = SimpleNamespace(id=42, message="Test comment", photo=None, media=None,
                                  get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Test", last_name=None, username="example")))
        with patch.object(app, "authorized", AsyncMock(return_value=object())), patch.object(app, "load_comments", AsyncMock(return_value=[message])) as loader:
            first = await app.draw(app.Draw(url="https://t.me/example/7"))
            second = await app.draw(app.Draw(url="https://t.me/example/7"))
            self.assertEqual(first["url"], "https://t.me/example/7?comment=42")
            self.assertEqual(second["count"], 1)
            self.assertEqual(loader.await_count, 1)

    async def test_page_refresh_collects_comments_again(self):
        app.cached_url = None
        message = SimpleNamespace(id=42, message="Test", photo=None, media=None,
                                  get_sender=AsyncMock(return_value=None))
        with patch.object(app, "authorized", AsyncMock(return_value=object())), patch.object(app, "load_comments", AsyncMock(return_value=[message])) as loader:
            first_page = app.Draw(url="https://t.me/example/7", page_session_id="page-one")
            await app.draw(first_page)
            await app.draw(first_page)
            self.assertEqual(loader.await_count, 1)
            await app.draw(app.Draw(url="https://t.me/example/7", page_session_id="page-two"))
            self.assertEqual(loader.await_count, 2)

    async def test_failed_import_never_commits_partial_list(self):
        app.cached_url = "https://t.me/previous/1"
        app.comments = ["previous"]
        with patch.object(app, "authorized", AsyncMock(return_value=object())), patch.object(app, "load_comments", AsyncMock(side_effect=ConnectionError)):
            with self.assertRaises(ConnectionError):
                await app.draw(app.Draw(url="https://t.me/example/7"))
        self.assertEqual(app.cached_url, "https://t.me/previous/1")
        self.assertEqual(app.comments, ["previous"])

    def test_browser_request_guards(self):
        with TestClient(app.app, base_url="http://127.0.0.1:8894") as browser:
            self.assertEqual(browser.get("/").status_code, 200)
            self.assertEqual(browser.get("/", headers={"host": "evil.example"}).status_code, 403)
            self.assertEqual(browser.post("/api/draw", json={"url": "https://t.me/example/7"}, headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_pages_can_draw_but_cannot_authorize_account(self):
        headers = {"Origin": app.PAGES_ORIGIN}
        with TestClient(app.app, base_url="http://127.0.0.1:8894") as browser:
            preflight = browser.options("/api/draw", headers=headers)
            self.assertEqual(preflight.status_code, 200)
            self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], app.PAGES_ORIGIN)
            self.assertEqual(browser.get("/api/status", headers=headers).headers["Access-Control-Allow-Origin"], app.PAGES_ORIGIN)
            self.assertEqual(browser.post("/api/login", headers=headers, json={}).status_code, 403)
            self.assertEqual(browser.options("/api/draw", headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_secret_files_and_internal_docs_are_not_served(self):
        with TestClient(app.app, base_url="http://127.0.0.1:8894") as browser:
            for path in ["/credentials.json", "/telegram.session", "/.env", "/.git/config", "/app.py",
                         "/docs", "/redoc", "/openapi.json", "/%2e%2e/credentials.json"]:
                self.assertEqual(browser.get(path).status_code, 404, path)

    def test_invalid_login_never_echoes_secrets(self):
        secret = "PRIVATE_TEST_VALUE"
        with TestClient(app.app, base_url="http://127.0.0.1:8894") as browser:
            response = browser.post("/api/login", headers={"Origin": "http://127.0.0.1:8894"},
                                    json={"api_id": "invalid", "api_hash": secret, "phone": secret})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)

    def test_large_and_non_json_requests_are_rejected(self):
        with TestClient(app.app, base_url="http://127.0.0.1:8894") as browser:
            origin = {"Origin": app.PAGES_ORIGIN}
            self.assertEqual(browser.post("/api/draw", headers=origin, json={"url": "x" * 9000}).status_code, 413)
            self.assertEqual(browser.post("/api/draw", headers=origin, content="url=test").status_code, 415)

    def test_origin_and_host_impostors_are_rejected(self):
        with TestClient(app.app, base_url="http://127.0.0.1:8894") as browser:
            for origin in ["null", "https://grigand96-hash.github.io.evil.example", "https://evil.example", "http://localhost:9999"]:
                response = browser.post("/api/draw", headers={"Origin": origin}, json={"url": "https://t.me/example/7"})
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
            self.assertEqual(browser.get("/", headers={"Host": "127.0.0.1.evil.example:8894"}).status_code, 403)

    async def test_random_selection_preserves_chosen_message_and_link(self):
        messages = [SimpleNamespace(id=number, message="<script>test</script>", photo=None, media=None,
                                   get_sender=AsyncMock(return_value=None)) for number in [10, 20, 30]]
        app.cached_url = None
        with patch.object(app, "authorized", AsyncMock(return_value=object())), patch.object(app, "load_comments", AsyncMock(return_value=messages)):
            for message in messages:
                with patch.object(app.secrets, "choice", return_value=message) as choose:
                    result = await app.draw(app.Draw(url="https://t.me/example/7", page_session_id="random-check"))
                    choose.assert_called_once_with(messages)
                    self.assertEqual(result["url"], f"https://t.me/example/7?comment={message.id}")
                    self.assertEqual(result["count"], 3)
                    self.assertEqual(result["text"], "<script>test</script>")


if __name__ == "__main__":
    unittest.main()
