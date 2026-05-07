"""Tests for chatwire_telegram._helpers pure functions.

No python-telegram-bot interaction here — _helpers.py has no bot-library
imports. We import the submodule directly; chatwire (and therefore chat_db
and integrations.base) must be installed or on sys.path.
"""
from __future__ import annotations

import pytest

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
from chat_db import InboundMessage
from integrations.base import SendOutcome, SendTarget


# ---------------------------------------------------------------------------
# helpers shared across new test classes
# ---------------------------------------------------------------------------

def _msg(**kwargs) -> InboundMessage:
    """Minimal InboundMessage for _chat_tag tests."""
    defaults = dict(rowid=1, handle="", text="", attachments=[], is_from_me=False)
    defaults.update(kwargs)
    return InboundMessage(**defaults)


def _target(label: str = "Alice", kind: str = "handle", value: str = "+15550001111") -> SendTarget:
    return SendTarget(kind=kind, value=value, label=label)


def _outcome(status: str, fell_back_to_sms: bool = False, hint: str = "") -> SendOutcome:
    return SendOutcome(status=status, hint=hint, service="iMessage",
                       fell_back_to_sms=fell_back_to_sms)


# ---------------------------------------------------------------------------
# _chunk_for_telegram
# ---------------------------------------------------------------------------

class TestChunkForTelegram:
    def test_empty_string_returns_empty_placeholder(self):
        assert _chunk_for_telegram("") == ["(empty)"]

    def test_short_text_returned_as_single_chunk(self):
        assert _chunk_for_telegram("hello") == ["hello"]

    def test_text_exactly_n_chars_is_single_chunk(self):
        s = "x" * 10
        assert _chunk_for_telegram(s, n=10) == [s]

    def test_text_one_over_n_splits_at_n_when_no_newline(self):
        s = "x" * 11
        result = _chunk_for_telegram(s, n=10)
        assert len(result) == 2
        assert result[0] == "x" * 10
        assert result[1] == "x"

    def test_splits_at_last_newline_before_n(self):
        # "aaaa\nbbbb" with n=6: newline at index 4 (>= n//2=3) → split there
        s = "aaaa\nbbbb"
        result = _chunk_for_telegram(s, n=6)
        assert result[0] == "aaaa"
        assert result[1] == "bbbb"

    def test_newline_too_early_falls_back_to_hard_split(self):
        # newline at index 1 (< n//2=5 for n=10) → hard split at 10.
        # The \n stays inside chunk 0; the remainder is stripped of leading \n.
        s = "a\n" + "b" * 18
        result = _chunk_for_telegram(s, n=10)
        assert result[0] == s[:10]           # "a\nbbbbbbbb"
        assert result[1] == "b" * 10         # continuation, leading \n stripped

    def test_leading_newlines_stripped_from_subsequent_chunks(self):
        s = "a" * 5 + "\n" + "b" * 5
        result = _chunk_for_telegram(s, n=6)
        assert result[1] == "b" * 5  # no leading \n

    def test_three_chunks(self):
        s = "a" * 5 + "\n" + "b" * 5 + "\n" + "c" * 5
        result = _chunk_for_telegram(s, n=6)
        assert len(result) == 3
        assert result[0] == "a" * 5
        assert result[1] == "b" * 5
        assert result[2] == "c" * 5

    def test_default_n_is_telegram_chunk_constant(self):
        # Verify the default is wired to the constant, not hard-coded elsewhere.
        import inspect
        sig = inspect.signature(_chunk_for_telegram)
        assert sig.parameters["n"].default == TELEGRAM_CHUNK


# ---------------------------------------------------------------------------
# _slug
# ---------------------------------------------------------------------------

class TestSlug:
    def test_lowercase_ascii(self):
        assert _slug("alice") == "alice"

    def test_spaces_become_underscores(self):
        assert _slug("Alice Smith") == "alice_smith"

    def test_uppercase_lowered(self):
        assert _slug("BOB") == "bob"

    def test_special_chars_stripped(self):
        assert _slug("anne-marie") == "anne_marie"

    def test_dots_stripped(self):
        assert _slug("j.doe") == "j_doe"

    def test_leading_trailing_underscores_stripped(self):
        assert _slug("_foo_") == "foo"

    def test_truncated_to_28_chars(self):
        long = "a" * 40
        assert _slug(long) == "a" * 28

    def test_empty_string_returns_contact(self):
        assert _slug("") == "contact"

    def test_all_special_chars_returns_contact(self):
        assert _slug("!!!") == "contact"

    def test_numbers_preserved(self):
        assert _slug("user123") == "user123"

    def test_mixed_unicode_non_alnum_becomes_underscore(self):
        # "é" is non-ASCII → replaced by "_" → trailing "_" stripped → "caf"
        result = _slug("café")
        assert result == "caf"
        # Key invariant: output contains only a-z0-9_
        import re
        assert re.fullmatch(r"[a-z0-9_]*", result)


# ---------------------------------------------------------------------------
# _looks_like_group_guid
# ---------------------------------------------------------------------------

class TestLooksLikeGroupGuid:
    def test_imessage_group_guid(self):
        assert _looks_like_group_guid("iMessage;+;chat629abc123") is True

    def test_sms_group_guid(self):
        assert _looks_like_group_guid("SMS;+;chat000111222") is True

    def test_one_to_one_imessage_guid(self):
        # 1:1 uses ;-; not ;+;
        assert _looks_like_group_guid("iMessage;-;+15551234567") is False

    def test_plain_phone_handle(self):
        assert _looks_like_group_guid("+15551234567") is False

    def test_email_handle(self):
        assert _looks_like_group_guid("user@example.com") is False

    def test_random_string(self):
        assert _looks_like_group_guid("not a guid") is False

    def test_strips_whitespace(self):
        assert _looks_like_group_guid("  iMessage;+;chat123  ") is True

    def test_sms_without_chat_segment(self):
        assert _looks_like_group_guid("SMS;+;5551234567") is False


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------

class TestParseDuration:
    def test_hours(self):
        assert _parse_duration("1h") == 3600

    def test_minutes(self):
        assert _parse_duration("30m") == 1800

    def test_days(self):
        assert _parse_duration("2d") == 172800

    def test_seconds_suffix(self):
        assert _parse_duration("120s") == 120

    def test_bare_integer(self):
        assert _parse_duration("90") == 90

    def test_empty_string_returns_none(self):
        assert _parse_duration("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_duration("   ") is None

    def test_fractional_hours(self):
        assert _parse_duration("2.5h") == 9000

    def test_fractional_minutes(self):
        assert _parse_duration("1.5m") == 90

    def test_uppercase_unit(self):
        assert _parse_duration("1H") == 3600

    def test_invalid_suffix(self):
        assert _parse_duration("5x") is None

    def test_non_numeric_bare(self):
        assert _parse_duration("abc") is None

    def test_zero_seconds(self):
        assert _parse_duration("0s") == 0

    def test_large_value(self):
        assert _parse_duration("7d") == 604800


# ---------------------------------------------------------------------------
# _fmt_capability
# ---------------------------------------------------------------------------

class TestFmtCapability:
    def test_no_services_returns_never_contacted(self):
        assert _fmt_capability([]) == "never contacted"

    def test_no_services_with_outcomes_still_never_contacted(self):
        assert _fmt_capability([], {"iMessage": {"total": 5}}) == "never contacted"

    # --- SMS only ---

    def test_sms_only_no_history(self):
        assert _fmt_capability(["SMS"]) == "SMS only (untested 30d)"

    def test_sms_only_sent_successfully(self):
        assert _fmt_capability(
            ["SMS"], {"SMS": {"total": 3, "latest_error": 0}}
        ) == "SMS ✓ 3 sent 30d"

    def test_sms_only_with_error(self):
        assert _fmt_capability(
            ["SMS"], {"SMS": {"total": 2, "latest_error": 5}}
        ) == "SMS err=5 (2 sent 30d)"

    # --- iMessage configured, not recently tested ---

    def test_imessage_untested_no_sms(self):
        assert _fmt_capability(["iMessage"]) == "iMessage configured (untested 30d)"

    def test_imessage_untested_with_sms_history(self):
        result = _fmt_capability(
            ["iMessage", "SMS"],
            {"iMessage": {}, "SMS": {"total": 4, "latest_error": 0}},
        )
        assert result == "iMessage configured (untested 30d), SMS 4 sent"

    # --- iMessage with delivery history ---

    def test_imessage_delivered(self):
        result = _fmt_capability(
            ["iMessage"],
            {"iMessage": {"total": 10, "delivered": 8, "latest_error": 0,
                          "latest_rowid": 99}},
        )
        assert result == "iMessage ✓ 8/10 30d"

    def test_imessage_delivered_with_sms_tail(self):
        result = _fmt_capability(
            ["iMessage", "SMS"],
            {
                "iMessage": {"total": 10, "delivered": 8, "latest_error": 0,
                             "latest_rowid": 99},
                "SMS": {"total": 2, "latest_error": 0},
            },
        )
        assert result == "iMessage ✓ 8/10 30d, SMS 2 sent"

    def test_imessage_sent_not_delivered(self):
        result = _fmt_capability(
            ["iMessage"],
            {"iMessage": {"total": 5, "delivered": 0, "latest_error": 3,
                          "latest_rowid": 50}},
        )
        assert result == "iMessage 5 sent / 0 delivered 30d, latest err=3"

    # --- iMessage deregistered (err=22) ---

    def test_imessage_deregistered_sms_working(self):
        result = _fmt_capability(
            ["iMessage"],
            {
                "iMessage": {"total": 0, "latest_error": 22},
                "SMS": {"total": 5, "latest_error": 0},
            },
        )
        assert result == "iMessage deregistered → SMS ✓ (5 sent 30d)"

    def test_imessage_deregistered_sms_error(self):
        result = _fmt_capability(
            ["iMessage"],
            {
                "iMessage": {"total": 0, "latest_error": 22},
                "SMS": {"total": 2, "latest_error": 7},
            },
        )
        assert result == "iMessage deregistered → SMS err=7"

    def test_imessage_deregistered_no_sms(self):
        result = _fmt_capability(
            ["iMessage"],
            {"iMessage": {"latest_error": 22}},
        )
        assert result == "iMessage deregistered (SMS untested)"

    # --- unknown services ---

    def test_unknown_single_service(self):
        assert _fmt_capability(["FaceTime"]) == "FaceTime (unknown)"

    def test_unknown_multiple_services(self):
        assert _fmt_capability(["FaceTime", "WhatsApp"]) == "FaceTime+WhatsApp (unknown)"

    # --- outcomes=None default ---

    def test_outcomes_none_treated_as_empty(self):
        # No outcomes → iMessage untested
        result = _fmt_capability(["iMessage"], None)
        assert result == "iMessage configured (untested 30d)"


# ---------------------------------------------------------------------------
# _chat_tag
# ---------------------------------------------------------------------------

class TestChatTag:
    def test_named_group_returned_verbatim(self):
        assert _chat_tag(_msg(chat_name="Friends")) == "Friends"

    def test_named_group_open_bracket_replaced(self):
        assert _chat_tag(_msg(chat_name="My [Group]")) == "My (Group)"

    def test_named_group_only_open_bracket(self):
        assert _chat_tag(_msg(chat_name="[test]")) == "(test)"

    def test_named_group_multiple_brackets(self):
        assert _chat_tag(_msg(chat_name="[A] and [B]")) == "(A) and (B)"

    def test_unnamed_group_uses_last_six_of_identifier(self):
        # "chat629180424750381661".removeprefix("chat") = "629180424750381661"
        # [-6:] = "381661"
        result = _chat_tag(_msg(chat_identifier="chat629180424750381661"))
        assert result == "Group 381661"

    def test_unnamed_group_short_identifier(self):
        # "chat12".removeprefix("chat") = "12", [-6:] = "12"
        result = _chat_tag(_msg(chat_identifier="chat12"))
        assert result == "Group 12"

    def test_unnamed_group_empty_identifier_falls_back_to_question_mark(self):
        result = _chat_tag(_msg(chat_identifier=""))
        assert result == "Group ?"

    def test_unnamed_group_none_identifier_falls_back_to_question_mark(self):
        # chat_identifier defaults to "" in InboundMessage, so test explicit empty
        result = _chat_tag(_msg())
        assert result == "Group ?"

    def test_named_group_takes_precedence_over_identifier(self):
        result = _chat_tag(_msg(chat_name="Named", chat_identifier="chat999999"))
        assert result == "Named"


# ---------------------------------------------------------------------------
# _format_send_reply
# ---------------------------------------------------------------------------

class TestFormatSendReply:
    def test_delivered_no_sms_fallback(self):
        r = _format_send_reply("sent", _target("Alice"), _outcome("delivered"))
        assert r == "✓ delivered → Alice"

    def test_delivered_with_sms_fallback(self):
        r = _format_send_reply("sent", _target("Bob"), _outcome("delivered", fell_back_to_sms=True))
        assert r == "✓ delivered via SMS → Bob"

    def test_sent_awaiting_receipt(self):
        r = _format_send_reply("sent", _target("Carol"), _outcome("sent"))
        assert r == "✓ sent → Carol (awaiting delivery receipt)"

    def test_sent_photo_awaiting_receipt(self):
        r = _format_send_reply("sent photo", _target("Dave"), _outcome("sent"))
        assert r == "✓ sent photo → Dave (awaiting delivery receipt)"

    def test_sent_via_sms_awaiting_receipt(self):
        r = _format_send_reply("sent", _target("Eve"), _outcome("sent", fell_back_to_sms=True))
        assert r == "✓ sent via SMS → Eve (awaiting delivery receipt)"

    def test_pending_with_hint(self):
        r = _format_send_reply("sent", _target("Frank"), _outcome("pending", hint="Messages.app busy"))
        assert r == "⏳ sent → Frank, pending — Messages.app busy"

    def test_failed_with_hint(self):
        r = _format_send_reply("sent", _target("Grace"), _outcome("failed", hint="send error 4"))
        assert r == "⚠️ NOT delivered → Grace: send error 4"

    def test_failed_empty_hint_uses_default(self):
        r = _format_send_reply("sent", _target("Hank"), _outcome("failed", hint=""))
        assert r == "⚠️ NOT delivered → Hank: unknown error"

    def test_label_with_spaces_preserved(self):
        r = _format_send_reply("sent", _target("First Last"), _outcome("delivered"))
        assert r == "✓ delivered → First Last"

    def test_group_target_label(self):
        tgt = _target(label="Work Chat", kind="chat", value="iMessage;+;chat123")
        r = _format_send_reply("sent", tgt, _outcome("sent"))
        assert r == "✓ sent → Work Chat (awaiting delivery receipt)"
