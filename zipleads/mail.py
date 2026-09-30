"""Daily lead email: a short summary in the body, the CSV attached."""

from __future__ import annotations

import smtplib
import sqlite3
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

from zipleads.config import Settings

BODY_ROWS = 15


def build_message(
    settings: Settings, rows: list[sqlite3.Row], csv_path: Path, territory_name: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = ", ".join(settings.mail_to)
    msg["Subject"] = f"zipleads: {len(rows)} new leads, {territory_name}"
    lines = [f"{len(rows)} unsubmitted leads for {territory_name}, best first.", ""]
    for r in rows[:BODY_ROWS]:
        flags = [s for s in r["signals"].split(",") if s.startswith("flag:")]
        flag_txt = f"  [{', '.join(f.split(':', 1)[1] for f in flags)}]" if flags else ""
        who = f"  {r['contact_name']} ({r['contact_title']})" if r["contact_name"] else ""
        phone = f"  {r['phone']}" if r["phone"] else ""
        lines.append(
            f"{r['score']:>3}  {r['company_name']}  |  {r['address'] or r['city']}"
            f"{phone}{who}{flag_txt}"
        )
    if len(rows) > BODY_ROWS:
        lines.append(f"... {len(rows) - BODY_ROWS} more in the attached CSV.")
    lines += ["", "Full detail, evidence links and dedupe keys are in the attachment."]
    msg.set_content("\n".join(lines))
    msg.add_attachment(
        csv_path.read_bytes(), maintype="text", subtype="csv", filename=csv_path.name
    )
    return msg


def send(settings: Settings, msg: EmailMessage, smtp_factory: Callable = smtplib.SMTP_SSL) -> None:
    with smtp_factory(settings.smtp_host, settings.smtp_port) as smtp:
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
