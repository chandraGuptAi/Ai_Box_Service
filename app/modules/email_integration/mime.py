"""Builds the RFC 5322 message for a Gmail draft: plain text + HTML alternatives, plus an optional attachment.

Python's ``email`` package does the encoding (headers, base64, boundaries); nothing is concatenated by hand.
Header values are stripped of CR/LF first so a subject or name can never inject extra headers.
"""
from __future__ import annotations

from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type-only: providers/gmail.py imports this module
    from .providers.base import OutgoingEmail


def clean_header(value: str) -> str:
    return " ".join((value or "").replace("\r", " ").replace("\n", " ").split())


def build_mime(email: OutgoingEmail) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = Address(display_name=clean_header(email.from_name or ""), addr_spec=email.from_email)
    if email.to:   # a draft may have no recipients yet; the user adds them in Gmail
        msg["To"] = ", ".join(email.to)
    msg["Subject"] = clean_header(email.subject)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=email.from_email.rpartition("@")[2] or None)
    msg.set_content(email.text)
    msg.add_alternative(email.html, subtype="html")
    if email.attachment:
        maintype, _, subtype = email.attachment.content_type.partition("/")
        msg.add_attachment(email.attachment.data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=clean_header(email.attachment.filename))
    return msg.as_bytes()
