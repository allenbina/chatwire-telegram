"""Pure helper functions for the Telegram integration.

No python-telegram-bot dependency here. stdlib + chat_db + integrations.base
only, so this module is safe to import in test environments that don't have
the bot library installed.
"""
from __future__ import annotations

import re

from chat_db import InboundMessage
from integrations.base import SendOutcome, SendTarget

# iMessage messages are capped at 4 000 chars when relayed to Telegram.
TELEGRAM_CHUNK = 4000


def _chunk_for_telegram(text: str, n: int = TELEGRAM_CHUNK) -> list[str]:
    if not text:
        return ["(empty)"]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= n:
            parts.append(remaining)
            break
        split = remaining.rfind("\n", 0, n)
        if split < n // 2:
            split = n
        parts.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    return parts


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:28] or "contact"


def _looks_like_group_guid(s: str) -> bool:
    """AppleScript chat GUIDs look like "iMessage;+;chat123…" or "SMS;+;chat…".

    The distinguishing marker is the `;+;` (or `;-;` for 1:1) segment. We only
    treat the group form as a group here; 1:1 GUIDs shouldn't appear in user
    input for the whitelist.
    """
    s = s.strip()
    return (s.startswith("iMessage;") or s.startswith("SMS;")) and ";+;chat" in s


def _parse_duration(s: str) -> int | None:
    """Parse '1h', '30m', '2d', '120s', or bare seconds into seconds."""
    s = s.strip().lower()
    if not s:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in units:
        try:
            return int(float(s[:-1]) * units[s[-1]])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


def _fmt_capability(
    services: list[str],
    outcomes: dict[str, dict] | None = None,
) -> str:
    """Honest capability label — reflects current reachability, not config.

    `services` is what's configured in chat.db's handle table. `outcomes` is
    actual per-service send results (see ChatDBReader.outcomes_for). Apple
    leaves iMessage handle rows around after deregistration, so `services`
    alone would say "iMessage works" for contacts who haven't in months —
    `outcomes` corrects that by surfacing err=22 lineage and SMS fallbacks.
    """
    outcomes = outcomes or {}
    im = outcomes.get("iMessage") or {}
    sms = outcomes.get("SMS") or {}
    has_im_cfg = "iMessage" in services
    has_sms_cfg = "SMS" in services
    if not services:
        return "never contacted"

    im_total = im.get("total", 0)
    im_delivered = im.get("delivered", 0)
    im_latest_err = im.get("latest_error", 0)
    sms_total = sms.get("total", 0)
    sms_latest_err = sms.get("latest_error", 0)

    if has_im_cfg:
        if im_latest_err == 22:
            if sms_total > 0 and sms_latest_err == 0:
                return f"iMessage deregistered → SMS ✓ ({sms_total} sent 30d)"
            if sms_total > 0:
                return f"iMessage deregistered → SMS err={sms_latest_err}"
            return "iMessage deregistered (SMS untested)"
        if im_total == 0 and "latest_rowid" not in im:
            if has_sms_cfg and sms_total > 0:
                return f"iMessage configured (untested 30d), SMS {sms_total} sent"
            return "iMessage configured (untested 30d)"
        if im_delivered > 0:
            tail = f", SMS {sms_total} sent" if sms_total > 0 else ""
            return f"iMessage ✓ {im_delivered}/{im_total} 30d{tail}"
        return f"iMessage {im_total} sent / 0 delivered 30d, latest err={im_latest_err}"

    if has_sms_cfg:
        if sms_total == 0:
            return "SMS only (untested 30d)"
        if sms_latest_err == 0:
            return f"SMS ✓ {sms_total} sent 30d"
        return f"SMS err={sms_latest_err} ({sms_total} sent 30d)"

    return "+".join(services) + " (unknown)"


def _chat_tag(msg: InboundMessage) -> str:
    """Stable, user-visible label for a group chat.

    Named groups use their display_name verbatim, with brackets replaced —
    the prefix parser keys on `[...]` delimiters, so embedded brackets would
    confuse the reply round-trip. Unnamed groups fall back to a short tag
    derived from chat_identifier so they're still unique.
    """
    if msg.chat_name:
        return msg.chat_name.replace("[", "(").replace("]", ")")
    ident = msg.chat_identifier or ""
    tail = ident[4:] if ident.startswith("chat") else ident
    short = tail[-6:] or "?"
    return f"Group {short}"


def _format_send_reply(verb: str, target: SendTarget, r: SendOutcome) -> str:
    """Human reply for /send, slug sends, photo sends. verb ∈ {'sent', 'sent photo'}."""
    label = target.label
    via = " via SMS" if r.fell_back_to_sms else ""
    if r.status == "delivered":
        return f"✓ delivered{via} → {label}"
    if r.status == "sent":
        return f"✓ {verb}{via} → {label} (awaiting delivery receipt)"
    if r.status == "pending":
        return f"⏳ {verb}{via} → {label}, pending — {r.hint}"
    return f"⚠️ NOT delivered → {label}: {r.hint or 'unknown error'}"
