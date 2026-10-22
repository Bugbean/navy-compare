"""Outbound email, in one place.

Welcome and approval emails, feedback notifications and alerts all go through
here. The Cloudflare User-Agent workaround below is the reason there is one
transport and not several: it was found once, at the cost of every app-sent
email failing silently, and a second copy is a second chance to lose it.
"""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.resend.com/emails"

# The sender domain is verified with Resend; changing it without verifying the
# new one means every send returns 403.
DEFAULT_FROM = "Navy Compare <noreply@example.invalid>"   # set MAIL_FROM per environment


def configured() -> bool:
    """Whether mail can be sent from this service at all.

    Worth asking before an action whose only effect is an email, so the caller
    can say "not configured" instead of reporting a success that never left.
    """
    return bool(os.environ.get("RESEND_API_KEY", ""))


def send(to: list[str], subject: str, html: str, *,
         reply_to: str | None = None, sender: str | None = None,
         cc: list[str] | None = None, bcc: list[str] | None = None) -> bool:
    """Send one email. Returns whether Resend accepted it.

    Never raises. Every caller here is doing something else as its real work —
    signing someone in, storing a bug report — and none of them should fail
    because a notification could not be delivered.
    """
    key = os.environ.get("RESEND_API_KEY", "")
    if not (key and to):
        log.warning("Email not sent (RESEND_API_KEY or recipients unset): %s", subject)
        return False

    payload = {"from": sender or os.environ.get("MAIL_FROM", DEFAULT_FROM),
               "to": to, "subject": subject, "html": html}
    if reply_to:
        payload["reply_to"] = reply_to
    # A copied address is visible to everyone on the message; a blind one is
    # not. Worth keeping apart deliberately, because the difference is the
    # whole point of having both and is not recoverable once sent.
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc

    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Required: Resend's API sits behind Cloudflare, whose bot check
            # answers Python's default "Python-urllib/x.y" signature with a
            # 403 (Cloudflare error 1010). Without this every app-sent email
            # silently fails.
            "User-Agent": "navy-compare/1.0",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except urllib.error.HTTPError as e:
        # Log Resend's own explanation — a bare "403" can mean a bot block, an
        # unverified sender domain or a revoked key, which need different fixes.
        log.error("Resend send failed: HTTP %s: %s", e.code,
                  e.read().decode(errors="replace")[:300])
        return False
    except Exception as e:                              # noqa: BLE001
        log.error("Resend send failed: %s", e)
        return False
