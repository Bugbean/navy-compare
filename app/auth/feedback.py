"""Requests and bug reports from the people using the app.

The context is taken from the session and the request — who, which page, which
URL, which browser — and never from what the person types. Someone reporting
that a card looks wrong should describe the card, not their own whereabouts,
and a report that arrives without them is a report nobody can reproduce.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from html import escape

from . import mailer

log = logging.getLogger(__name__)

# Where reports go. Unset means stored but not emailed.
TO = [a.strip() for a in
      os.environ.get("FEEDBACK_EMAIL", "").split(",") if a.strip()]

KINDS = {"request": "Feature request", "bug": "Bug report"}

MAX_MESSAGE = 4000
MIN_MESSAGE = 10


class Invalid(ValueError):
    """What the person typed cannot be filed, and they should be told why."""


def validate(kind: str, message: str) -> tuple[str, str]:
    """Check a submission, returning the cleaned pair or raising Invalid."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise Invalid("Choose whether this is a request or a bug.")
    message = (message or "").strip()
    if len(message) < MIN_MESSAGE:
        raise Invalid("Add a little more detail — a sentence or two is plenty.")
    return kind, message[:MAX_MESSAGE]


def _row(kind, message, *, user, site, page_key, page_url, user_agent) -> dict:
    return {
        "kind": kind,
        "message": message,
        "site": site if site in ("web",) else None,
        "page_key": (page_key or "")[:60] or None,
        "page_url": (page_url or "")[:500] or None,
        "user_agent": (user_agent or "")[:300] or None,
        "user_id": getattr(user, "id", None),
        "user_email": getattr(user, "email", None),
        "user_name": getattr(user, "display_name", None) or getattr(user, "full_name", None),
        # Written rather than left to the column default, so the row says what
        # it means on its own: stored, nobody told yet.
        "emailed": False,
    }


def _email_html(row: dict, when: str) -> str:
    def cell(label, value):
        return (f'<tr><td style="padding:3px 14px 3px 0;color:#666;'
                f'white-space:nowrap;vertical-align:top">{escape(label)}</td>'
                f'<td style="padding:3px 0">{escape(str(value or "—"))}</td></tr>')

    who = row.get("user_name") or "Unknown"
    if row.get("user_email"):
        who = f'{who} ({row["user_email"]})'

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
        'sans-serif;font-size:14px;line-height:1.55;color:#111;max-width:620px">'
        f'<p style="margin:0 0 4px;font-size:12px;letter-spacing:.06em;'
        f'text-transform:uppercase;color:#888">{escape(KINDS[row["kind"]])}</p>'
        # The message first and whole: everything else is provenance, and the
        # person reading this at 11pm wants the sentence, not the metadata.
        f'<div style="white-space:pre-wrap;padding:13px 15px;background:#f6f6f6;'
        f'border-left:3px solid #1f4e8c;border-radius:4px;margin:0 0 18px">'
        f'{escape(row["message"])}</div>'
        '<table style="border-collapse:collapse;font-size:13px">'
        + cell("From", who)
        + cell("Page", row.get("page_key"))
        + cell("URL", row.get("page_url"))
        + cell("When", when)
        + cell("Browser", row.get("user_agent"))
        + '</table></div>'
    )


def submit(sb, kind: str, message: str, *, user, site: str,
           page_key: str = "", page_url: str = "", user_agent: str = "") -> dict:
    """Store a report and notify. Returns what to tell the person.

    Stored first, then emailed, and the order matters: if Resend is down or
    unconfigured, the report still exists and `emailed` stays false, which is a
    state that can be found and acted on later. Emailing first and failing to
    store loses the report entirely and tells the person it worked.
    """
    kind, message = validate(kind, message)
    row = _row(kind, message, user=user, site=site, page_key=page_key,
               page_url=page_url, user_agent=user_agent)

    try:
        stored = sb.table("feedback").insert(row).execute().data
        row_id = (stored or [{}])[0].get("id")
    except Exception as exc:                            # noqa: BLE001
        log.error("Feedback not stored: %s", exc)
        raise Invalid("That didn't save. Try again in a moment.") from exc

    when = datetime.now(timezone.utc).astimezone().strftime("%a %d %b %Y, %I:%M %p %Z")
    subject = f"[Navy Compare {KINDS[kind]}] {message.splitlines()[0][:70]}"
    # Replying to the notification reaches the person who reported it, which is
    # usually the next thing anyone wants to do.
    sent = mailer.send(TO, subject, _email_html(row, when),
                       reply_to=row.get("user_email") or None)
    if sent and row_id:
        try:
            sb.table("feedback").update({"emailed": True}).eq("id", row_id).execute()
        except Exception as exc:                        # noqa: BLE001
            log.warning("Feedback %s emailed but not marked: %s", row_id, exc)

    return {"ok": True, "id": row_id,
            "message": "Thanks — that's logged." if sent else
                       "Thanks — that's logged. (The email notification didn't go out, "
                       "but the report is saved.)"}
