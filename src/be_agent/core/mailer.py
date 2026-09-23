"""메일 발송. SMTP 설정이 없으면 서버 로그에 출력한다 (로컬 개발용)."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from be_agent.core.config import Settings

logger = logging.getLogger(__name__)


def _send_smtp(settings: Settings, message: EmailMessage) -> None:
    assert settings.smtp_host
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


async def send_email(settings: Settings, *, to: str, subject: str, body: str) -> None:
    if not settings.smtp_host:
        logger.warning("SMTP 미설정 — 메일 대신 로그로 출력합니다.\nTo: %s\nSubject: %s\n\n%s", to, subject, body)
        return

    message = EmailMessage()
    message["From"] = settings.smtp_from or settings.smtp_username or "no-reply@localhost"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        await asyncio.to_thread(_send_smtp, settings, message)
    except Exception:
        # 백그라운드 작업이라 사용자에게 에러를 돌려줄 수 없다. 로그로 남긴다.
        logger.exception("메일 발송 실패 (to=%s)", to)
