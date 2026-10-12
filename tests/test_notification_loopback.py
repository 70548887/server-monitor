"""使用本机捕获端验证真实通知协议，数据不会发送给外部收件人。"""
import asyncio
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock

from app.services.notification import EmailNotificationService, WebhookNotificationService


async def test_smtp_transport_and_html_escaping():
    captured = []
    async def smtp(reader, writer):
        writer.write(b"220 localhost test SMTP\r\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                if line.startswith((b"EHLO", b"HELO")):
                    writer.write(b"250-localhost\r\n250 AUTH PLAIN LOGIN\r\n")
                elif line.startswith(b"AUTH"):
                    writer.write(b"235 Authenticated\r\n")
                elif line.startswith(b"DATA"):
                    writer.write(b"354 End with dot\r\n")
                    await writer.drain()
                    captured.append(await reader.readuntil(b"\r\n.\r\n"))
                    writer.write(b"250 Captured locally\r\n")
                elif line.startswith(b"QUIT"):
                    writer.write(b"221 Bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 OK\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(smtp, "127.0.0.1", 0)
    async with server:
        ok, error = await EmailNotificationService.send_email(
            "127.0.0.1", server.sockets[0].getsockname()[1], "test@example.invalid", "fixture-only",
            ["capture@example.invalid"], "local verification", '<img src=x onerror="bad()">', False,
        )
    assert ok, error
    assert len(captured) == 1
    message = BytesParser(policy=policy.default).parsebytes(captured[0])
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "<img src=x" not in html and "&lt;img src=x" in html


async def test_webhook_rechecks_recovery_before_retry(monkeypatch):
    requests = []
    async def http(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = next(int(v.split(b":",1)[1]) for v in headers.split(b"\r\n") if v.lower().startswith(b"content-length:"))
        requests.append(await reader.readexactly(length))
        writer.write(b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(http, "127.0.0.1", 0)
    check = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(WebhookNotificationService, "_retry_delay", lambda *_: 0)
    async with server:
        ok, reason = await WebhookNotificationService.send_webhook(
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/capture", {"event":"down"}, can_send=check)
    assert not ok and "取消" in reason
    assert len(requests) == 1
    assert check.await_count == 2
