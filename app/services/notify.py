"""In-app notifications plus optional push channels (ntfy, Discord, webhook, email)."""
from __future__ import annotations

import smtplib
import threading
from email.message import EmailMessage

import httpx
from sqlalchemy.orm import Session

from .. import settings
from ..models import Notification


def send(db: Session, kind: str, title: str, body: str = "", link: str | None = None,
         push: bool = True) -> Notification:
    n = Notification(kind=kind, title=title[:500], body=body, link=link)
    db.add(n)
    db.commit()
    if push:
        threading.Thread(target=_push, args=(kind, title, body, link), daemon=True).start()
    return n


def _abs(link: str | None) -> str | None:
    base = (settings.get("PUBLIC_URL") or "").rstrip("/")
    return f"{base}{link}" if (link and base) else link


def _push(kind: str, title: str, body: str, link: str | None) -> list[str]:
    results = []
    url = _abs(link)
    ntfy = settings.get("NTFY_URL")
    if ntfy:
        try:
            headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore")[:250], "Tags": kind}
            if url:
                headers["Click"] = url
            httpx.post(ntfy, content=body.encode(), headers=headers, timeout=15)
            results.append("ntfy: ok")
        except Exception as exc:
            results.append(f"ntfy: {exc}")
    discord = settings.get("DISCORD_WEBHOOK_URL")
    if discord:
        try:
            content = f"**{title}**\n{body}" + (f"\n{url}" if url else "")
            httpx.post(discord, json={"content": content[:1900]}, timeout=15)
            results.append("discord: ok")
        except Exception as exc:
            results.append(f"discord: {exc}")
    hook = settings.get("WEBHOOK_URL")
    if hook:
        try:
            httpx.post(hook, json={"kind": kind, "title": title, "body": body, "link": url, "text": f"{title}\n{body}"},
                       timeout=15)
            results.append("webhook: ok")
        except Exception as exc:
            results.append(f"webhook: {exc}")
    host, to = settings.get("SMTP_HOST"), settings.get("SMTP_TO")
    if host and to:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = settings.get("SMTP_FROM") or settings.get("SMTP_USER") or "flipdesk@localhost"
            msg["To"] = to
            msg.set_content(body + (f"\n\n{url}" if url else ""))
            port = int(settings.get("SMTP_PORT") or 587)
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                s = smtplib.SMTP(host, port, timeout=20)
                if settings.get("SMTP_STARTTLS"):
                    s.starttls()
            if settings.get("SMTP_USER"):
                s.login(settings.get("SMTP_USER"), settings.get("SMTP_PASSWORD"))
            s.send_message(msg)
            s.quit()
            results.append("email: ok")
        except Exception as exc:
            results.append(f"email: {exc}")
    return results


def test_all() -> list[str]:
    return _push("test", "FlipDesk test alert", "If you can read this, alerts are working.", "/") or [
        "No channels configured"]
