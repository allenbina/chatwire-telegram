"""Telegram integration plugin for chatwire.

Renders inbound iMessage events into a single Telegram chat with
`From <name>:` prefixes, and turns Telegram messages back into outbound
iMessage sends (replies, /send, /<contact-slug>, photo uploads, inline
whitelist search).

Install:
    pip install chatwire-telegram
    pipx inject chatwire chatwire-telegram

Config block (under `integrations.telegram` in config.json, or via env
TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS for legacy):

    {
        "enabled": true,
        "bot_token": "123456:abc...",
        "allowed_user_ids": [12345678]
    }

The first allowed_user_id is the delivery target chat. Inline mode must be
enabled on the bot via BotFather (/setinline) for /whitelist search to work.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand, InlineQueryResultArticle, InputTextMessageContent, Update,
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, InlineQueryHandler,
    MessageHandler, filters,
)

from prefix import ReplyTarget, format_inbound, parse_reply_target
from whitelist import (
    add as wl_add, add_group as wl_add_group,
    all_handles as wl_all, all_groups as wl_all_groups,
    remove as wl_remove, remove_group as wl_remove_group,
)
from integrations.base import (
    BridgeContext, InboundMessage, SendOutcome, SendTarget,
)
from chatwire_telegram._helpers import (
    TELEGRAM_CHUNK,
    _chat_tag,
    _chunk_for_telegram,
    _fmt_capability,
    _format_send_reply,
    _looks_like_group_guid,
    _parse_duration,
    _slug,
)

log = logging.getLogger("chatwire.telegram")

# iMessage's "object replacement character" — placeholder body iMessage uses
# when a row is purely an attachment carrier. Don't relay it as text.
ORC = "￼"

# Plain-text Telegram messages with no /send and no reply-to fall back to
# the most-recent target if it's still within this TTL.
STICKY_TTL_S = 600  # 10 min

_UNRESOLVED_GROUP_HINT = (
    "Can't resolve the group chat this reply points to — I only remember group "
    "tags from messages relayed since the bridge last started. Pick the group "
    "from /menu or send a fresh message in the group first."
)


class TelegramIntegration:
    NAME = "telegram"

    SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "default": False,
                "title": "Enable Telegram integration",
            },
            "bot_token": {
                "type": "string",
                "title": "Bot token",
                "description": "From @BotFather. Treat as a secret.",
            },
            "allowed_user_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "title": "Allowed Telegram user IDs",
                "description": (
                    "Numeric Telegram user IDs allowed to control the bridge. "
                    "The first ID receives all relayed inbound messages."
                ),
                "minItems": 1,
            },
        },
        "required": ["bot_token", "allowed_user_ids"],
    }

    def __init__(self, config: dict[str, Any]):
        self._token: str = config.get("bot_token", "") or ""
        self._allowed_user_ids: set[int] = {int(x) for x in config.get("allowed_user_ids") or []}

        self._ctx: BridgeContext | None = None
        self._app: Application | None = None
        self._target_chat_id: int | None = None

        # When set to a future epoch second, on_inbound is a no-op until then.
        self._mute_until: float = 0.0

        # chat_name tag (lowercased) -> chat GUID. Populated whenever we relay
        # an inbound group message so replies in Telegram can route to the
        # right chat without carrying a GUID in the visible prefix. Lost on
        # restart; rebuilds as new messages arrive.
        self._chat_guid_by_tag: dict[str, str] = {}

        self._last_target: tuple[SendTarget, float] | None = None

    # ---------- lifecycle ----------

    async def start(self, ctx: BridgeContext) -> None:
        if not self._token:
            raise ValueError("telegram integration: bot_token is required")
        if not self._allowed_user_ids:
            raise ValueError("telegram integration: allowed_user_ids must contain at least one ID")
        self._ctx = ctx
        # Single delivery target: the first allowlisted user. (POC has one anyway.)
        self._target_chat_id = next(iter(self._allowed_user_ids))

        app = Application.builder().token(self._token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        app.add_handler(CommandHandler("handles", self._cmd_handles))
        app.add_handler(CommandHandler("refresh_contacts", self._cmd_refresh_contacts))
        app.add_handler(CommandHandler("mute", self._cmd_mute))
        app.add_handler(CommandHandler("unmute", self._cmd_unmute))
        app.add_handler(CommandHandler("send", self._cmd_send))
        app.add_handler(CommandHandler("whitelist", self._cmd_whitelist))
        app.add_handler(CommandHandler("whitelist_add", self._cmd_whitelist_add))
        app.add_handler(CommandHandler("whitelist_remove", self._cmd_whitelist_remove))
        app.add_handler(CommandHandler("check", self._cmd_check))
        # Catch-all for dynamically-registered /<slug> contact commands.
        app.add_handler(MessageHandler(filters.COMMAND, self._cmd_slug))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        app.add_handler(InlineQueryHandler(self._inline_whitelist))

        await app.initialize()
        await app.start()
        await self._register_commands(app)
        await app.updater.start_polling(allowed_updates=["message", "inline_query"])
        self._app = app
        log.info("telegram integration started; allowed_user_ids=%s",
                 sorted(self._allowed_user_ids))

    async def stop(self) -> None:
        app = self._app
        if app is None:
            return
        try:
            await app.updater.stop()
        finally:
            await app.stop()
            await app.shutdown()
        self._app = None
        log.info("telegram integration stopped")

    # ---------- inbound: iMessage -> Telegram ----------

    async def on_inbound(self, msg: InboundMessage) -> None:
        if self._app is None or self._target_chat_id is None or self._ctx is None:
            return  # not started yet
        if time.time() < self._mute_until:
            return  # /mute is active

        body = msg.text.strip()
        # Drop the lone object-replacement char when it's just a placeholder
        # for an attachment-only message — the attachment send below carries
        # the info.
        if body == ORC and msg.attachments:
            body = ""

        name = self._ctx.name_for(msg.handle)

        chat_tag = _chat_tag(msg) if msg.is_group else ""
        if chat_tag and msg.chat_guid:
            self._chat_guid_by_tag[chat_tag.lower()] = msg.chat_guid

        quote_line = ""
        if msg.parent_text or msg.parent_handle:
            parent_name = (
                "you" if msg.parent_is_from_me
                else (self._ctx.name_for(msg.parent_handle) or msg.parent_handle or "?")
            )
            snippet = (msg.parent_text or "(attachment)").replace(ORC, "(attachment)").strip()
            if len(snippet) > 60:
                snippet = snippet[:60].rstrip() + "…"
            quote_line = f"↪ {parent_name}: {snippet}\n"

        if body or quote_line:
            text = quote_line + format_inbound(
                msg.handle, name, body or "(no text)", chat_name=chat_tag or None,
            )
            for chunk in _chunk_for_telegram(text):
                await self._app.bot.send_message(chat_id=self._target_chat_id, text=chunk)

        if not msg.is_from_me:
            if msg.is_group:
                self._remember_last_target(SendTarget(
                    kind="chat", value=msg.chat_guid, label=chat_tag,
                ))
            else:
                label = self._ctx.name_for(msg.handle) or msg.handle
                self._remember_last_target(SendTarget(
                    kind="handle", value=msg.handle, label=label,
                ))

        for att in msg.attachments:
            if not att.ready:
                await self._app.bot.send_message(
                    chat_id=self._target_chat_id,
                    text=f"(attachment from {msg.handle} not yet downloaded: {att.path.name})",
                )
                continue
            try:
                caption = f"From {self._ctx.name_for(msg.handle) or msg.handle}"
                with att.path.open("rb") as fh:
                    if att.mime_type == "image/gif" or att.path.suffix.lower() == ".gif":
                        await self._app.bot.send_animation(
                            chat_id=self._target_chat_id, animation=fh, caption=caption,
                        )
                    elif att.mime_type.startswith("image/"):
                        await self._app.bot.send_photo(
                            chat_id=self._target_chat_id, photo=fh, caption=caption,
                        )
                    elif att.mime_type.startswith("video/"):
                        await self._app.bot.send_video(
                            chat_id=self._target_chat_id, video=fh, caption=caption,
                        )
                    else:
                        await self._app.bot.send_document(
                            chat_id=self._target_chat_id, document=fh, caption=caption,
                        )
            except Exception:
                log.exception("failed to send attachment %s", att.path)

    # ---------- outbound: Telegram -> iMessage ----------

    def _remember_last_target(self, target: SendTarget) -> None:
        self._last_target = (target, time.time())

    def _get_sticky_target(self) -> SendTarget | None:
        if self._last_target is None:
            return None
        tgt, t = self._last_target
        if time.time() - t > STICKY_TTL_S:
            return None
        return tgt

    def _resolve_chat_tag(self, tag: str) -> str | None:
        return self._chat_guid_by_tag.get(tag.lower())

    def _target_from_reply(self, rt: ReplyTarget) -> SendTarget | None:
        """Turn a parsed reply prefix into a concrete SendTarget.

        If the prefix carried a [Group] tag and we've seen that tag before,
        route the reply to the group. Otherwise route to the sender's handle.
        Unresolved group tags return None so the caller can warn rather than
        silently DM the sender (the exact footgun we're guarding against).
        """
        if rt.chat_name:
            guid = self._resolve_chat_tag(rt.chat_name)
            if guid:
                return SendTarget(kind="chat", value=guid, label=rt.chat_name)
            return None
        label = (self._ctx.name_for(rt.handle) if self._ctx else None) or rt.handle
        return SendTarget(kind="handle", value=rt.handle, label=label)

    async def _resolve_target(self, update: Update, body: str) -> tuple[SendTarget | None, str]:
        """Return (target, body_after_command_prefix). target=None means could
        not resolve.

        Reply-to-a-relayed-message is the primary routing signal: if the
        replied text carries a [Group] tag, the reply goes back to that
        group; otherwise it goes to the sender's handle. /send <handle>
        <body> is always a 1:1 send (no group syntax; use the /menu slug for
        groups). Finally, the sticky target from the most recent inbound is
        used as fallback.
        """
        msg = update.effective_message
        if msg.reply_to_message and msg.reply_to_message.text:
            rt = parse_reply_target(msg.reply_to_message.text)
            if rt:
                target = self._target_from_reply(rt)
                if target:
                    return target, body
        if body.startswith("/send"):
            parts = body.split(maxsplit=2)
            if len(parts) >= 2:
                handle = parts[1]
                rest = parts[2] if len(parts) == 3 else ""
                label = (self._ctx.name_for(handle) if self._ctx else None) or handle
                return SendTarget(kind="handle", value=handle, label=label), rest
        sticky = self._get_sticky_target()
        if sticky:
            return sticky, body
        return None, body

    # ---------- auth helpers ----------

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id in self._allowed_user_ids

    async def _reject(self, update: Update) -> None:
        user = update.effective_user
        log.warning("rejected user_id=%s name=%s",
                    user.id if user else None,
                    user.full_name if user else None)
        if update.effective_message:
            await update.effective_message.reply_text("Not authorized.")

    # ---------- command handlers ----------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        await update.effective_message.reply_text(
            "iMessage bridge online.\n"
            "Reply to a relayed message, or use /send <handle> <body>.\n"
            "/whoami for IDs, /handles to see relay scope."
        )

    async def _cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        u = update.effective_user
        c = update.effective_chat
        await update.effective_message.reply_text(
            f"user_id={u.id}\nchat_id={c.id}\nallowed={sorted(self._allowed_user_ids)}"
        )

    async def _cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        arg = (update.effective_message.text or "").partition(" ")[2].strip() or "1h"
        secs = _parse_duration(arg)
        if not secs or secs <= 0:
            await update.effective_message.reply_text("usage: /mute <duration>  (e.g. 30m, 2h, 1d)")
            return
        self._mute_until = time.time() + secs
        until_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._mute_until))
        await update.effective_message.reply_text(f"muted relay until {until_str} ({secs}s)")

    async def _cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        self._mute_until = 0.0
        await update.effective_message.reply_text("relay unmuted")

    async def _cmd_refresh_contacts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        if self._ctx is None:
            return
        n = await asyncio.to_thread(self._ctx.reload_contacts)
        await self._register_commands(context.application)
        await update.effective_message.reply_text(f"contacts reloaded: {n} handles; menu refreshed")

    async def _cmd_handles(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        scope = self._relay_scope()
        groups = wl_all_groups()
        await update.effective_message.reply_text(
            "self: " + (", ".join(sorted(scope["self"])) or "(none)") +
            "\nwhitelist: " + (", ".join(sorted(wl_all())) or "(none)") +
            f"\ngroups: {len(groups)} (use /whitelist to list)"
        )

    async def _cmd_whitelist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        handles = sorted(wl_all())
        groups = sorted(wl_all_groups())
        if not handles and not groups:
            await update.effective_message.reply_text("whitelist empty")
            return
        lines = []
        if handles:
            lines.append(f"handles ({len(handles)}):")
            lines.extend(self._capability_lines(handles))
        if groups:
            if lines:
                lines.append("")
            lines.append(f"groups ({len(groups)}):")
            lines.extend(self._group_lines(groups))
        await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_whitelist_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        arg = (update.effective_message.text or "").partition(" ")[2].strip()
        if not arg:
            await update.effective_message.reply_text(
                "usage: /whitelist_add <handle or contact name or group GUID>"
            )
            return
        handles, groups = self._resolve_whitelist_input(arg)
        added_h = [h for h in handles if wl_add(h)]
        added_g = [g for g in groups if wl_add_group(g)]
        await self._register_commands(context.application)
        if not added_h and not added_g:
            await update.effective_message.reply_text("nothing added (already whitelisted or unknown)")
            return
        lines: list[str] = []
        if added_h:
            lines.append(f"added {len(added_h)} handle(s):")
            lines.extend(self._capability_lines(added_h))
        if added_g:
            if lines:
                lines.append("")
            lines.append(f"added {len(added_g)} group(s):")
            lines.extend(self._group_lines(added_g))
        await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_whitelist_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        arg = (update.effective_message.text or "").partition(" ")[2].strip()
        if not arg:
            await update.effective_message.reply_text(
                "usage: /whitelist_remove <handle or contact name or group GUID>"
            )
            return
        handles, groups = self._resolve_whitelist_input(arg)
        removed_h = [h for h in handles if wl_remove(h)]
        removed_g = [g for g in groups if wl_remove_group(g)]
        await self._register_commands(context.application)
        parts: list[str] = []
        if removed_h:
            contacts = self._ctx.contacts if self._ctx else {}
            names = sorted({contacts.get(h, h) for h in removed_h})
            parts.append(f"removed {len(removed_h)} handle(s): " + ", ".join(names))
        if removed_g:
            parts.append(f"removed {len(removed_g)} group(s)")
        if parts:
            await update.effective_message.reply_text("\n".join(parts))
        else:
            await update.effective_message.reply_text("nothing removed (not on whitelist)")

    async def _cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report iMessage/SMS capability for a handle or contact name.

        `/check <handle>` — checks that exact handle.
        `/check <name>`   — expands to every known handle for that contact.
        No arg — lists capability for the whole whitelist.
        """
        if not self._authorized(update):
            await self._reject(update); return
        arg = (update.effective_message.text or "").partition(" ")[2].strip()
        if arg:
            handles, _ = self._resolve_whitelist_input(arg)
            if not handles:
                await update.effective_message.reply_text("no handles resolved from that input")
                return
        else:
            handles = sorted(wl_all())
            if not handles:
                await update.effective_message.reply_text(
                    "whitelist empty — /check <handle or name> to check one"
                )
                return
        await update.effective_message.reply_text("\n".join(self._capability_lines(handles)))

    async def _cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or self._ctx is None:
            await self._reject(update); return
        msg = update.effective_message
        target, body = await self._resolve_target(update, msg.text or "")
        if not target or not body:
            await msg.reply_text("usage: /send <handle> <body>"); return
        try:
            r = await self._ctx.send_text(target, body)
            self._remember_last_target(target)
            self._ctx.mirror("outbound", kind="text",
                             handle=target.value if not target.is_group else "",
                             chat_guid=target.value if target.is_group else None,
                             text=body)
            await msg.reply_text(_format_send_reply("sent", target, r))
        except Exception as e:
            log.exception("send_text failed")
            await msg.reply_text(f"send failed: {type(e).__name__}: {e}")

    async def _cmd_slug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Dynamic command handler: any /<slug> for a known contact or group
        sets it as sticky target and echoes who's selected. User then types
        body on next line."""
        if not self._authorized(update) or self._ctx is None:
            await self._reject(update); return
        slug = (update.effective_message.text or "")[1:].split(maxsplit=1)[0].lower()
        body_part = ""
        parts = (update.effective_message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            body_part = parts[1]
        mapping = dict(self._relay_commands())
        target = mapping.get(slug)
        if not target:
            return  # fall through to other handlers / ignored
        if body_part.strip():
            body_text = body_part.strip()
            try:
                r = await self._ctx.send_text(target, body_text)
                self._remember_last_target(target)
                self._ctx.mirror("outbound", kind="text",
                                 handle=target.value if not target.is_group else "",
                                 chat_guid=target.value if target.is_group else None,
                                 text=body_text)
                await update.effective_message.reply_text(_format_send_reply("sent", target, r))
            except Exception as e:
                log.exception("slug send failed")
                await update.effective_message.reply_text(f"send failed: {type(e).__name__}: {e}")
        else:
            self._remember_last_target(target)
            kind_hint = "group" if target.is_group else target.value
            await update.effective_message.reply_text(
                f"target set: {target.label} ({kind_hint})\njust type to send."
            )

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reply-to-context text send (no /send prefix)."""
        if not self._authorized(update) or self._ctx is None:
            await self._reject(update); return
        msg = update.effective_message
        body = (msg.text or "").strip()
        if not body:
            return
        # If the user replied to a group message whose tag we can't resolve,
        # refuse instead of silently DMing the sender.
        if msg.reply_to_message and msg.reply_to_message.text:
            rt = parse_reply_target(msg.reply_to_message.text)
            if rt and rt.chat_name and not self._resolve_chat_tag(rt.chat_name):
                await msg.reply_text(_UNRESOLVED_GROUP_HINT)
                return
        target, body = await self._resolve_target(update, body)
        if not target:
            await msg.reply_text(
                "No target. Reply to a relayed message, or use /send <handle> <body>."
            )
            return
        try:
            r = await self._ctx.send_text(target, body)
            self._remember_last_target(target)
            self._ctx.mirror("outbound", kind="text",
                             handle=target.value if not target.is_group else "",
                             chat_guid=target.value if target.is_group else None,
                             text=body)
            await msg.reply_text(_format_send_reply("sent", target, r))
        except Exception as e:
            log.exception("send_text failed")
            await msg.reply_text(f"send failed: {type(e).__name__}: {e}")

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or self._ctx is None:
            await self._reject(update); return
        msg = update.effective_message
        caption = (msg.caption or "").strip()
        if msg.reply_to_message and msg.reply_to_message.text:
            rt = parse_reply_target(msg.reply_to_message.text)
            if rt and rt.chat_name and not self._resolve_chat_tag(rt.chat_name):
                await msg.reply_text(_UNRESOLVED_GROUP_HINT)
                return
        target, body = await self._resolve_target(update, caption)
        if not target:
            await msg.reply_text(
                "No target. Reply to a relayed message, or include `/send <handle>` in the caption."
            )
            return

        photo = msg.photo[-1]  # largest
        file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await file.download_to_drive(custom_path=str(tmp_path))
            r_photo = await self._ctx.send_file(target, tmp_path)
            self._remember_last_target(target)
            self._ctx.mirror("outbound", kind="photo",
                             handle=target.value if not target.is_group else "",
                             chat_guid=target.value if target.is_group else None,
                             text=body or None)
            r_text: SendOutcome | None = None
            if body:
                r_text = await self._ctx.send_text(target, body)
            # Report the worse of (photo, text) so failures aren't masked by
            # the other succeeding.
            rank = {"delivered": 0, "sent": 1, "pending": 2, "failed": 3}
            worst = max(
                [r for r in (r_photo, r_text) if r is not None],
                key=lambda r: rank.get(r.status, 0),
            )
            await msg.reply_text(_format_send_reply("sent photo", target, worst))
        except Exception as e:
            log.exception("photo send failed")
            await msg.reply_text(f"photo send failed: {type(e).__name__}: {e}")
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    async def _inline_whitelist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Inline typeahead over Contacts + group chats. Selecting a result
        pre-fills `/whitelist_add <name_or_guid>` into the chat; user sends to
        confirm. Requires inline mode enabled on the bot via BotFather
        (/setinline).
        """
        q = update.inline_query
        if q is None or q.from_user is None or q.from_user.id not in self._allowed_user_ids:
            if q is not None:
                await q.answer([], cache_time=1, is_personal=True)
            return
        needle = (q.query or "").strip().lower()
        already_handles = wl_all()
        already_groups = wl_all_groups()
        results: list[InlineQueryResultArticle] = []
        contacts = self._ctx.contacts if self._ctx else {}

        # Contacts: one entry per unique display name, matching the haystack.
        names = sorted({n for n in contacts.values() if n}, key=str.lower)
        if needle:
            names = [n for n in names if needle in n.lower()]
        for i, name in enumerate(names[:25]):
            same_name_handles = {h for h, nm in contacts.items() if nm == name}
            on_list = bool(same_name_handles & already_handles)
            suffix = "  ✓ already on whitelist" if on_list else ""
            results.append(InlineQueryResultArticle(
                id=f"wl-c-{i}",
                title=name + suffix,
                description="Contact — tap to prefill /whitelist_add",
                input_message_content=InputTextMessageContent(f"/whitelist_add {name}"),
            ))

        # Groups: named iMessage/SMS group chats from chat.db, filtered by
        # needle against name or participant handles. Unnamed groups fall
        # back to a synthetic label so they're still selectable.
        if self._ctx is not None:
            try:
                groups = self._ctx.list_groups()
            except Exception:
                log.exception("list_groups failed in inline search")
                groups = []
            filtered = []
            for g in groups:
                hay = " ".join([
                    g.get("name") or "",
                    g.get("chat_identifier") or "",
                    " ".join(g.get("participants") or []),
                ]).lower()
                if not needle or needle in hay:
                    filtered.append(g)
            for i, g in enumerate(filtered[:25]):
                name = g.get("name") or ""
                participants = g.get("participants") or []
                if name:
                    title = f"[Group] {name}"
                else:
                    sample = [
                        contacts.get((p or "").lower(), p or "") for p in participants[:3]
                    ]
                    title = f"[Group] (unnamed) {', '.join(sample) or '(empty)'}"
                on_list = g["guid"] in already_groups
                if on_list:
                    title += "  ✓ already on whitelist"
                desc = f"{len(participants)} members — tap to prefill /whitelist_add"
                results.append(InlineQueryResultArticle(
                    id=f"wl-g-{i}",
                    title=title[:100],
                    description=desc,
                    input_message_content=InputTextMessageContent(
                        f"/whitelist_add {g['guid']}"
                    ),
                ))
        await q.answer(results, cache_time=1, is_personal=True)

    # ---------- whitelist input parsing ----------

    def _resolve_whitelist_input(self, s: str) -> tuple[list[str], list[str]]:
        """User typed a handle, a Contacts display name, or a group GUID.

        Returns (handles_to_add, groups_to_add). Names expand to every known
        handle for that contact. Group GUIDs stay as-is. A bare literal that
        doesn't match a name is treated as a single literal handle (unchanged
        from the old single-target behavior).
        """
        s = s.strip()
        if not s:
            return [], []
        if _looks_like_group_guid(s):
            return [], [s]
        contacts = self._ctx.contacts if self._ctx else {}
        low = s.lower()
        matches = [h for h, name in contacts.items() if name.lower() == low]
        return (matches if matches else [low]), []

    # ---------- capability rendering ----------

    def _capability_lines(self, handles: list[str]) -> list[str]:
        """Format each handle as `• <label> — <capability>` for a TG reply."""
        contacts = self._ctx.contacts if self._ctx else {}
        if self._ctx is None:
            return [f"• {contacts.get(h, h)} ({h})" for h in handles]
        svc = self._ctx.services_for(handles)
        outcomes = self._ctx.outcomes_for(handles)
        out = []
        for h in handles:
            hl = h.lower()
            cap = _fmt_capability(svc.get(hl, []), outcomes.get(hl))
            name = contacts.get(hl)
            label = f"{name} ({h})" if name else h
            out.append(f"• {label} — {cap}")
        return out

    def _group_lines(self, guids: list[str]) -> list[str]:
        """One line per group: label, participant count. Pulls the
        display_name from the chat_tag map if we've seen the group since
        startup; otherwise asks chat.db."""
        if not guids:
            return []
        reverse_tag = {g: t for t, g in self._chat_guid_by_tag.items()}
        all_groups_info: dict[str, dict] = {}
        if self._ctx is not None:
            try:
                for g in self._ctx.list_groups():
                    all_groups_info[g["guid"]] = g
            except Exception:
                log.exception("list_groups failed")
        out = []
        for g in guids:
            info = all_groups_info.get(g, {})
            name = info.get("name") or reverse_tag.get(g) or "(unnamed group)"
            participants = info.get("participants") or []
            p_note = f", {len(participants)} members" if participants else ""
            out.append(f"• [{name}]{p_note}")
        return out

    # ---------- /menu / slug commands ----------

    def _relay_scope(self) -> dict[str, set[str]]:
        """Look up the current relay scope from the context.

        The context exposes the SELF + whitelist union so the integration
        doesn't need to know how the bridge composes them.
        """
        if self._ctx is None:
            return {"self": set(), "handles": set(), "groups": set()}
        return self._ctx.relay_scope()

    def _relay_commands(self) -> list[tuple[str, SendTarget]]:
        """Return [(slug, SendTarget)] for every relay-scope person and
        whitelisted group, with the canonical (most-recent) handle picked per
        display name. Handles without a name are skipped — no useful slug to
        show in the /menu. Groups are prefixed `g_` so they don't collide
        with contact slugs and are visually distinguishable.
        """
        seen: dict[str, SendTarget] = {}
        scope = self._relay_scope()
        contacts = self._ctx.contacts if self._ctx else {}
        # Build name -> [handles] from contacts intersected with relay scope.
        by_name: dict[str, list[str]] = defaultdict(list)
        for h, n in contacts.items():
            if h in scope["handles"]:
                by_name[n].append(h)
        for name, hs in sorted(by_name.items(), key=lambda x: x[0].lower()):
            slug = _slug(name)
            i = 2
            unique = slug
            while unique in seen:
                unique = f"{slug}_{i}"
                i += 1
            seen[unique] = SendTarget(kind="handle", value=hs[0], label=name)
            if len(seen) >= 80:  # leave room for groups + /start etc.
                break

        # Whitelisted groups. Look up their display name from the chat_tag
        # map populated as messages arrive; if not yet seen, fall back to the
        # GUID tail so the slug is at least unique. Prefix with "g_" to mark
        # groups.
        reverse_tag: dict[str, str] = {g: t for t, g in self._chat_guid_by_tag.items()}
        for guid in sorted(scope["groups"]):
            tag = reverse_tag.get(guid) or guid.split(";")[-1][-8:]
            slug_base = "g_" + _slug(tag)
            i = 2
            unique = slug_base
            while unique in seen:
                unique = f"{slug_base}_{i}"
                i += 1
            seen[unique] = SendTarget(kind="chat", value=guid, label=tag)
            if len(seen) >= 90:
                break
        return list(seen.items())

    async def _register_commands(self, app: Application) -> None:
        """Publish the full /menu: built-in commands + one per whitelisted
        person."""
        cmds: list[BotCommand] = [
            BotCommand("start", "help"),
            BotCommand("whoami", "show IDs"),
            BotCommand("handles", "show relay scope"),
            BotCommand("refresh_contacts", "reload Contacts lookup"),
            BotCommand("mute", "silence relay for a duration"),
            BotCommand("unmute", "resume relay"),
            BotCommand("send", "send <handle> <body>"),
            BotCommand("whitelist", "show whitelist"),
            BotCommand("whitelist_add", "add <handle or name>"),
            BotCommand("whitelist_remove", "remove <handle or name>"),
            BotCommand("check", "iMessage/SMS capability for <handle or name>"),
        ]
        for slug, target in self._relay_commands():
            arrow = "→ group" if target.is_group else "→"
            cmds.append(BotCommand(slug, f"{arrow} {target.label}"))
        try:
            await app.bot.set_my_commands(cmds[:100])
            log.info("registered %d bot commands", min(100, len(cmds)))
        except Exception:
            log.exception("set_my_commands failed")
