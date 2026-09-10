from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from enoch.local_web.server import LocalWebHost, start_local_web
from enoch.local_web.settings import LocalWebSettings, load_local_web_settings
from enoch.local_web.shortlist import (
    extract_products,
    format_shortlist_followup,
    record_shortlist_from_task,
    resolve_followup_shortlist,
)
from enoch.tasks.queue import TaskJob


KETTLE_RESULT = """
Enoch prepared isolated workspace task-6.

I found three currently available variants under $50.

| Product / merchant | Exact variant | Price | Capacity |
|---|---|---:|---:|
| [Jettle Travel Electric Kettle](https://jettlecompany.com/products/jettle-electric-kettle?variant=1) — Jettle Online Store | Black | $49.99 USD | 450 ml |
| [Sakerplus Portable Travel Electric Tea Kettle](https://www.sakerplus.com/products/sakerplus-portable-travel-electric-tea-kettle?variant=2) — Sakerplus | 110V three-prong | $44.99 USD | 500 ml |
| [Nicewell Dual-Voltage Travel Kettle](https://homejoykids1.com/products/b0dbc889sc?variant=3) — Little Sparks | Dark blue; US cord | $43.21 USD | 370 ml |

My pick: the Nicewell is the lightest.
No cart or checkout was created.
"""


class LocalWebTests(unittest.TestCase):
    def test_extracts_products_from_markdown_table(self) -> None:
        products = extract_products(KETTLE_RESULT)

        self.assertEqual(len(products), 3)
        self.assertEqual(products[0].name, "Jettle Travel Electric Kettle")
        self.assertEqual(products[0].store, "Jettle Online Store")
        self.assertEqual(products[0].price, "$49.99 USD")
        self.assertIn("/products/", products[0].url)
        self.assertEqual(products[2].name, "Nicewell Dual-Voltage Travel Kettle")
        from enoch.local_web.shortlist import product_brand

        self.assertEqual(product_brand(products[0]), "Jettle")
        self.assertEqual(product_brand(products[1]), "Sakerplus")
        self.assertEqual(product_brand(products[2]), "Little Sparks")

    def test_rejects_non_loopback_bind(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "enoch.local_web.settings.read_section",
            return_value={"bind": "0.0.0.0", "port": "36624"},
        ):
            with self.assertRaisesRegex(ValueError, "loopback"):
                load_local_web_settings(Path(directory))

    def test_serves_shortlist_and_requires_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job = TaskJob(
                id=6,
                chat_id=42,
                text="Help me find a travel kettle under $50 USD",
                created_at="2026-09-08T23:24:59+00:00",
                status="completed",
                result=KETTLE_RESULT,
            )
            shortlist = record_shortlist_from_task(job, KETTLE_RESULT, root=root)
            self.assertIsNotNone(shortlist)
            replies: list[tuple[str, str, int | None]] = []
            settings = LocalWebSettings(
                enabled=True,
                host="127.0.0.1",
                port=0,
                token="test-token",
            )
            server = start_local_web(
                LocalWebHost(
                    root=root,
                    conversation_id=42,
                    handle_chat=lambda text, shortlist_id="", tab=None: (
                        replies.append((text, shortlist_id, tab)) or f"heard:{text}"
                    ),
                ),
                settings=settings,
            )
            self.assertIsNotNone(server)
            assert server is not None
            try:
                origin = f"http://127.0.0.1:{server.port}"
                with self.assertRaises(HTTPError) as denied:
                    urlopen(f"{origin}/api/shop/t6", timeout=2)
                self.assertEqual(denied.exception.code, 401)

                page = urlopen(
                    Request(
                        f"{origin}/shop/t6?token=test-token",
                        headers={"Authorization": "Bearer test-token"},
                    ),
                    timeout=2,
                )
                html = page.read().decode("utf-8")
                self.assertIn("/api/shop/", html)
                self.assertIn("/thumb/", html)
                self.assertIn("go to ", html)
                self.assertIn(" product page", html)
                self.assertIn("shortlist_id", html)

                payload = json.loads(
                    urlopen(
                        Request(
                            f"{origin}/api/shop/t6",
                            headers={"Authorization": "Bearer test-token"},
                        ),
                        timeout=2,
                    ).read()
                )
                self.assertEqual(payload["id"], "t6")
                self.assertEqual(len(payload["products"]), 3)
                self.assertEqual(payload["products"][0]["brand"], "Jettle")
                self.assertEqual(payload["products"][1]["brand"], "Sakerplus")

                chat = json.loads(
                    urlopen(
                        Request(
                            f"{origin}/api/chat",
                            data=json.dumps(
                                {
                                    "text": "more on 2",
                                    "shortlist_id": "t6",
                                    "tab": 2,
                                }
                            ).encode("utf-8"),
                            headers={
                                "Authorization": "Bearer test-token",
                                "Content-Type": "application/json",
                            },
                            method="POST",
                        ),
                        timeout=2,
                    ).read()
                )
                self.assertEqual(chat["status"], "accepted")
                deadline = time.time() + 2
                while time.time() < deadline and not replies:
                    time.sleep(0.01)
                self.assertEqual(replies, [("more on 2", "t6", 2)])
            finally:
                server.stop()

    def test_conversation_turns_newest_first(self) -> None:
        from enoch.local_web.conversation import recent_conversation_turns
        from enoch.logs import log_conversation_turn

        with TemporaryDirectory() as directory:
            root = Path(directory)
            log_conversation_turn(
                chat_id=42, message="first", reply="one", root=root
            )
            log_conversation_turn(
                chat_id=42, message="second", reply="two", root=root
            )
            turns = recent_conversation_turns(root=root, chat_id="42")
            self.assertEqual(turns[0]["message"], "second")
            self.assertEqual(turns[-1]["message"], "first")

    def test_chat_post_does_not_block_conversation_poll(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def handle(text: str, shortlist_id: str = "", tab: int | None = None) -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = LocalWebSettings(
                enabled=True,
                host="127.0.0.1",
                port=0,
                token="test-token",
            )
            server = start_local_web(
                LocalWebHost(
                    root=root,
                    conversation_id=42,
                    handle_chat=handle,
                ),
                settings=settings,
            )
            self.assertIsNotNone(server)
            assert server is not None
            try:
                origin = f"http://127.0.0.1:{server.port}"
                t0 = time.time()
                chat = json.loads(
                    urlopen(
                        Request(
                            f"{origin}/api/chat",
                            data=json.dumps({"text": "hello"}).encode("utf-8"),
                            headers={
                                "Authorization": "Bearer test-token",
                                "Content-Type": "application/json",
                            },
                            method="POST",
                        ),
                        timeout=2,
                    ).read()
                )
                self.assertLess(time.time() - t0, 0.5)
                self.assertEqual(chat["status"], "accepted")
                self.assertTrue(started.wait(timeout=1))
                conversation = json.loads(
                    urlopen(
                        Request(
                            f"{origin}/api/conversation",
                            headers={"Authorization": "Bearer test-token"},
                        ),
                        timeout=2,
                    ).read()
                )
                self.assertIn("turns", conversation)
            finally:
                release.set()
                server.stop()

    def test_followup_prompt_lists_on_disk_products(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job = TaskJob(
                id=6,
                chat_id=42,
                text="Help me find a travel kettle under $50 USD",
                created_at="2026-09-08T23:24:59+00:00",
                status="completed",
                result=KETTLE_RESULT,
            )
            shortlist = record_shortlist_from_task(job, KETTLE_RESULT, root=root)
            self.assertIsNotNone(shortlist)
            assert shortlist is not None
            prompt = format_shortlist_followup(
                "which one has better review in these 2 kettles",
                shortlist,
                tab=2,
            )
            self.assertIn("Jettle Travel Electric Kettle", prompt)
            self.assertIn("Sakerplus Portable Travel Electric Tea Kettle", prompt)
            self.assertIn("currently viewing this tab", prompt)
            self.assertIn("which one has better review in these 2 kettles", prompt)
            loaded = resolve_followup_shortlist("t6", root=root)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.id, "t6")
            self.assertEqual(len(loaded.products), 3)

    def test_telegram_gets_local_shop_page_link(self) -> None:
        from enoch.local_web.settings import attach_local_shop_page_link

        message = attach_local_shop_page_link(
            "Task #6 final update",
            "http://127.0.0.1:36624/shop/t6?token=abc",
            shortlist_id="t6",
            title="Help me find a travel kettle under $50 USD",
        )
        self.assertTrue(message.startswith("Task #6 final update"))
        self.assertIn(
            "[Latest shop page (t6): Help me find a travel kettle under $50 USD]"
            "(http://127.0.0.1:36624/shop/t6?token=abc)",
            message,
        )
        self.assertIn("<!-- telegram:break -->", message)

    def test_extracts_open_graph_image(self) -> None:
        from enoch.local_web.thumbs import extract_preview_image_url

        html = """
        <html><head>
        <meta property="og:image" content="https://cdn.shopify.com/s/files/kettle.jpg">
        </head></html>
        """
        self.assertEqual(
            extract_preview_image_url(html, "https://jettlecompany.com/products/x"),
            "https://cdn.shopify.com/s/files/kettle.jpg",
        )

    def test_keeps_html_prefix_when_product_page_is_large(self) -> None:
        from io import BytesIO

        from enoch.local_web.thumbs import _read_limited

        class FakeBody:
            def __init__(self, data: bytes) -> None:
                self._buf = BytesIO(data)

            def read(self, size: int = -1) -> bytes:
                return self._buf.read(size)

        head = b'<meta property="og:image" content="https://cdn.example.com/k.png">'
        huge = head + b"x" * 800_000
        kept = _read_limited(FakeBody(huge), 512_000, keep_prefix=True)
        self.assertEqual(len(kept), 512_000)
        self.assertTrue(kept.startswith(head))
        dropped = _read_limited(FakeBody(huge), 512_000, keep_prefix=False)
        self.assertEqual(dropped, b"")

    def test_serves_product_thumbnail(self) -> None:
        from io import BytesIO

        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        html = (
            b'<meta property="og:image" content="https://cdn.example.com/k.png">'
        )

        class FakeResponse:
            def __init__(self, data: bytes, url: str) -> None:
                self._buf = BytesIO(data)
                self.url = url

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self, size: int = -1) -> bytes:
                return self._buf.read(size)

        def fake_urlopen(req: object, timeout: int = 0) -> FakeResponse:
            url = getattr(req, "full_url", str(req))
            if "cdn.example.com" in url:
                return FakeResponse(png, url)
            return FakeResponse(html, url)

        with TemporaryDirectory() as directory, patch(
            "enoch.local_web.thumbs.urlopen",
            side_effect=fake_urlopen,
        ):
            root = Path(directory)
            job = TaskJob(
                id=6,
                chat_id=42,
                text="Help me find a travel kettle under $50 USD",
                created_at="2026-09-08T23:24:59+00:00",
                status="completed",
                result=KETTLE_RESULT,
            )
            record_shortlist_from_task(job, KETTLE_RESULT, root=root)
            settings = LocalWebSettings(
                enabled=True,
                host="127.0.0.1",
                port=0,
                token="test-token",
            )
            server = start_local_web(
                LocalWebHost(
                    root=root,
                    conversation_id=42,
                    handle_chat=lambda text, shortlist_id="", tab=None: "ok",
                ),
                settings=settings,
            )
            self.assertIsNotNone(server)
            assert server is not None
            try:
                origin = f"http://127.0.0.1:{server.port}"
                body = urlopen(
                    Request(
                        f"{origin}/api/shop/t6/thumb/1",
                        headers={"Authorization": "Bearer test-token"},
                    ),
                    timeout=2,
                ).read()
                self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(body, png)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
