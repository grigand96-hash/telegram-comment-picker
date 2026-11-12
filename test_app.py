import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
