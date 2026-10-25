from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import hash_password
from app.db.session import Base, configure_sqlite
from app.main import app
from app.models import User


class BrowserProfile:
    """A shared cookie jar: multiple tabs in one browser profile use this object."""

    def __init__(self) -> None:
        self.cookie = ""

    async def request(self, method: str, url: str, form: dict[str, str] | None = None):
        parsed = urlsplit(url)
        payload = urlencode(form or {}).encode()
        headers = [(b"host", b"testserver")]
        if form is not None:
            headers.append((b"content-type", b"application/x-www-form-urlencoded"))
        if self.cookie:
            headers.append((b"cookie", self.cookie.encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": parsed.path, "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(), "root_path": "", "headers": headers,
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
        if "set-cookie" in response_headers:
            self.cookie = response_headers["set-cookie"].split(";", 1)[0]
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return start["status"], response_headers, body

    async def login(self, employee_id: str, password: str) -> None:
        status, headers, _ = await self.request("POST", "/login", {
            "employee_id": employee_id, "password": password,
        })
        if status != 303 or headers.get("location") != "/dashboard":
            raise AssertionError("Login did not redirect to dashboard")


class AuthenticationSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "auth.sqlite3"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.factory() as session:
            session.add_all([
                User(
                    employee_id="00000001", full_name="Первый менеджер", role="manager",
                    password_hash=hash_password("manager-password"), must_change_password=False,
                ),
                User(
                    employee_id="00000002", full_name="Второй руководитель", role="boss",
                    password_hash=hash_password("boss-password"), must_change_password=False,
                ),
            ])
            await session.commit()
        for target in ("app.main.SessionLocal", "app.db.session.SessionLocal"):
            patched = patch(target, self.factory)
            patched.start()
            self.addCleanup(patched.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def assert_dashboard_user(self, profile: BrowserProfile, expected: str, absent: str) -> None:
        status, _, body = await profile.request("GET", "/dashboard")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn(expected, text)
        self.assertNotIn(absent, text)

    async def test_tabs_of_one_profile_share_the_active_user(self):
        # Both logical tabs use the same profile and therefore the same cookie jar.
        shared_profile = BrowserProfile()
        first_tab = shared_profile
        second_tab = shared_profile

        await first_tab.login("00000001", "manager-password")
        await self.assert_dashboard_user(second_tab, "Первый менеджер", "Второй руководитель")

        await second_tab.login("00000002", "boss-password")
        await self.assert_dashboard_user(first_tab, "Второй руководитель", "Первый менеджер")

    async def test_separate_browser_profiles_keep_accounts_isolated(self):
        manager_profile = BrowserProfile()
        boss_profile = BrowserProfile()

        await manager_profile.login("00000001", "manager-password")
        await boss_profile.login("00000002", "boss-password")
        await self.assert_dashboard_user(manager_profile, "Первый менеджер", "Второй руководитель")
        await self.assert_dashboard_user(boss_profile, "Второй руководитель", "Первый менеджер")

        status, headers, _ = await manager_profile.request("GET", "/logout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        status, headers, _ = await manager_profile.request("GET", "/dashboard")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        await self.assert_dashboard_user(boss_profile, "Второй руководитель", "Первый менеджер")
