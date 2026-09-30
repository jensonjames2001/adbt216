"""Telegram probe: does the bot token work, which chat should the alerts go
to, and what does a sample alert with its two follow-up edits look like?

Run from the repo root:

    .venv/bin/python probes/probe_telegram.py                 # getMe + getUpdates: prints the chat ids it can see
    .venv/bin/python probes/probe_telegram.py --send          # also posts a sample alert and edits it twice
    .venv/bin/python probes/probe_telegram.py --send --chat-id -1001234567890

Needs TELEGRAM_BOT_TOKEN in .env. --send needs --chat-id or TELEGRAM_CHAT_ID.
Requests: 2 without --send, 5 with it (plus at most one retry per call after
an HTTP 429). Message the bot first, or add it to the group and post
something, otherwise getUpdates has nothing to show.

What is saved (fixtures/telegram/live/<timestamp>/): the token is never
written or printed; the saved copy of getUpdates keeps only update ids,
dates, chat types, group/channel ids and titles, and message text LENGTHS.
No names, no usernames, no message text, and no private-chat ids: a private
chat's id IS that person's Telegram user id (rule 2), so it is shown on the
screen only, because the owner needs their own to fill in TELEGRAM_CHAT_ID.
A group chat is the better choice: its id is nobody's user id.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from probes import common  # noqa: E402
from probes.common import (  # noqa: E402
    EXIT_FAILED, EXIT_MISSING_KEY, EXIT_OK, Findings, RequestBudget, explain_exception,
    london, missing_key, now_utc, redacted_url, save_response, secret, status_line,
    timed_request, write_json,
)

API_BASE = "https://api.telegram.org"
SAMPLE_LAT, SAMPLE_LON = 51.4356, -0.5115     # on the M25 near J13, for the buttons
MAX_RETRY_AFTER_S = 120                        # sleep at most this long on a 429
PAUSE_BETWEEN_EDITS_S = 3.0

# Update fields that hold a chat. Order matters only for which one is picked first.
_CHAT_HOLDERS = (
    "message", "edited_message", "channel_post", "edited_channel_post",
    "business_message", "edited_business_message", "my_chat_member",
    "chat_member", "chat_join_request",
)


# --------------------------------------------------------------------------
# Pure helpers (tested)
# --------------------------------------------------------------------------

def api_url(token: str, method: str) -> str:
    """Only ever log this through redacted_url()."""
    return f"{API_BASE}/bot{token}/{method}"


def chat_of(update: dict) -> tuple[str | None, dict | None]:
    """(holder field name, chat dict) for an update, or (kind, None)."""
    for key in _CHAT_HOLDERS:
        obj = update.get(key)
        if isinstance(obj, dict) and isinstance(obj.get("chat"), dict):
            return key, obj["chat"]
    cq = update.get("callback_query")
    if isinstance(cq, dict):
        msg = cq.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("chat"), dict):
            return "callback_query", msg["chat"]
    kind = next((k for k in update if k != "update_id"), None)
    return kind, None


def redact_chat(chat: dict) -> dict:
    """Keep id and type; keep title only for groups and channels."""
    out: dict[str, Any] = {"id": chat.get("id"), "type": chat.get("type")}
    if chat.get("type") in ("group", "supergroup", "channel") and chat.get("title") is not None:
        out["title"] = chat["title"]
    return out


def strip_private_ids(obj: Any) -> Any:
    """The on-disk rule: any chat dict of type 'private' loses its id (it is
    a Telegram user id). Everything else passes through unchanged. Applied
    to every redacted body just before it is written."""
    if isinstance(obj, dict):
        out = {k: strip_private_ids(v) for k, v in obj.items()}
        if out.get("type") == "private":
            out.pop("id", None)
        return out
    if isinstance(obj, list):
        return [strip_private_ids(v) for v in obj]
    return obj


def is_private_chat_id(chat_id: Any) -> bool:
    """Telegram chat ids: negative = group/supergroup/channel, '@name' = a
    public channel, positive = a private chat with one person (their user id)."""
    text = str(chat_id).strip()
    return not (text.startswith("-") or text.startswith("@"))


def chat_id_for_files(chat_id: Any) -> str:
    """The chat id as findings.md may show it."""
    return "(private chat: id shown on screen only)" if is_private_chat_id(chat_id) else str(chat_id)


def hint_for(status: int, description: str | None, method: str = "") -> str | None:
    """What to change, in plain words, for the Bot API errors a wrong .env
    produces. None when there is nothing specific to say."""
    desc = (description or "").strip().lower()
    if "chat not found" in desc or "chat_id is empty" in desc:
        return ("TELEGRAM_CHAT_ID in .env (or --chat-id) is not a chat this bot can see. Check the "
                "number, and for a group make sure the bot has been added to it. Run this probe "
                "without --send to list the chat ids the bot can see.")
    if status == 401 or "unauthorized" in desc:
        return ("TELEGRAM_BOT_TOKEN in .env is not a valid bot token. Copy it again from @BotFather "
                "(it looks like 123456789:AA...) and run again.")
    if status == 404 or desc == "not found":
        return ("TELEGRAM_BOT_TOKEN in .env is incomplete or malformed (Telegram answered 404 Not "
                "Found). It must be the whole token from @BotFather: digits, a colon, then letters, "
                "with no spaces or quotes.")
    if "blocked by the user" in desc:
        return "That person has blocked the bot. Open the bot in Telegram and press Start, or use a group."
    if "not a member" in desc or "kicked" in desc or "not enough rights" in desc:
        return "The bot is not (or no longer) in that group, or may not post there. Add it to the group again."
    if "can't parse entities" in desc:
        return "Telegram rejected the alert text as HTML. Send this findings.md back so the text can be fixed."
    return None


def redact_updates(updates: list[dict]) -> list[dict]:
    """The copy of getUpdates that is safe to save: no names, no usernames,
    no text, only what the owner needs to pick a chat id."""
    out = []
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        kind, chat = chat_of(upd)
        row: dict[str, Any] = {"update_id": upd.get("update_id"), "kind": kind}
        holder = upd.get(kind) if kind else None
        if isinstance(holder, dict):
            if holder.get("date") is not None:
                row["date"] = holder["date"]
            text = holder.get("text") or holder.get("caption") or ""
            row["text_len"] = len(text) if isinstance(text, str) else 0
        if chat is not None:
            row["chat"] = redact_chat(chat)
        out.append(row)
    return out


def distinct_chats(updates: list[dict]) -> list[dict]:
    seen: dict[Any, dict] = {}
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        _, chat = chat_of(upd)
        if chat is None or chat.get("id") is None:
            continue
        seen.setdefault(chat["id"], redact_chat(chat))
    return list(seen.values())


def redact_sent_message(data: dict) -> dict:
    """The safe copy of a sendMessage/editMessageText result."""
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return {"ok": data.get("ok") if isinstance(data, dict) else None}
    chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
    return {
        "ok": data.get("ok"),
        "message_id": result.get("message_id"),
        "date": result.get("date"),
        "edit_date": result.get("edit_date"),
        "chat": redact_chat(chat) if chat else None,
        "text_len": len(result.get("text") or ""),
        "has_reply_markup": "reply_markup" in result,
    }


def sample_alert_text(now_london: datetime) -> str:
    """The brief's alert format, word for word, with a test warning on top."""
    first = now_london - timedelta(minutes=3)
    return "\n".join([
        "This was a test message",
        "🚨 Reported breakdown: M25 clockwise",
        "J13 (Staines) to J14 (Heathrow T4)",
        f"Hard shoulder · first reported {first:%H:%M} (3 min ago)",
        "6.2 mi from Feltham in a straight line",
        "TomTom · 2 reports",
    ])


def replace_last_line(text: str, new_last: str) -> str:
    lines = text.split("\n")
    lines[-1] = new_last
    return "\n".join(lines)


def sample_buttons(lat: float = SAMPLE_LAT, lon: float = SAMPLE_LON) -> dict:
    return {"inline_keyboard": [[
        {"text": "Google Maps", "url": f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"},
        {"text": "Waze", "url": f"https://waze.com/ul?ll={lat},{lon}&navigate=yes"},
    ]]}


def retry_after_seconds(data: Any, default: float = 5.0) -> float:
    try:
        return float(data["parameters"]["retry_after"])
    except (KeyError, TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# One Bot API call
# --------------------------------------------------------------------------

class TelegramError(Exception):
    """An ok=false answer. ``hint`` is the plain-words fix, when there is one."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def call(
    client, token: str, method: str, payload: dict | None, *,
    budget: RequestBudget, findings: Findings, outdir: Path, stem: str,
    body_redactor: Callable[[dict], Any] | None = None,
) -> dict:
    """POST one Bot API method. Honours a 429 once (sleeps retry_after).
    Saves the meta always; saves the body only through body_redactor when one
    is given (bodies may carry personal data), and strips private-chat ids
    from that copy. Raises TelegramError (with a hint for the owner) when the
    API says ok=false, after saving the error body (errors carry no personal
    data). Network failures propagate as httpx exceptions."""
    url = api_url(token, method)
    findings.add(f"POST {redacted_url(url)}")
    for attempt in (1, 2):
        budget.count("telegram")
        response, took = timed_request(client, "POST", url, json=payload or {})
        findings.add(f"  {status_line(response, took)}")
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code == 429 and attempt == 1:
            wait = min(retry_after_seconds(data), MAX_RETRY_AFTER_S)
            findings.add(f"  HTTP 429: Telegram asks us to wait {wait:.0f} s; sleeping, then retrying once")
            save_response(outdir, f"{stem}.429", response, body=True, elapsed_s=took)
            time.sleep(wait)
            continue
        break

    ok = isinstance(data, dict) and data.get("ok") is True
    if ok and body_redactor is not None:
        save_response(outdir, stem, response, body=False, elapsed_s=took, note="body saved redacted")
        write_json(outdir / f"{stem}.body.redacted.json", strip_private_ids(body_redactor(data)))
    elif ok:
        save_response(outdir, stem, response, body=False, elapsed_s=took, note="body not saved")
    else:
        save_response(outdir, stem, response, body=True, elapsed_s=took)
        desc = data.get("description") if isinstance(data, dict) else None
        raise TelegramError(f"{method} failed: HTTP {response.status_code}, {desc or 'no JSON description'}",
                            hint_for(response.status_code, desc, method))
    return data


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    cfg, outdir, parser = common.setup(
        "telegram",
        "Check the Telegram bot token, list the chats the bot can see (to copy a chat id) "
        "and optionally send a sample alert with its two follow-up edits.",
        argv=argv, max_requests_default=10,
    )
    parser.add_argument("--send", action="store_true",
                        help="send the sample alert to --chat-id / TELEGRAM_CHAT_ID and edit it twice")
    parser.add_argument("--chat-id", default=None, metavar="ID",
                        help="chat to send to (a number; negative for groups); default: TELEGRAM_CHAT_ID from .env")
    args = parser.parse_args(argv)

    token = secret("TELEGRAM_BOT_TOKEN")
    if not token:
        missing_key("TELEGRAM_BOT_TOKEN", outdir)
    chat_id = args.chat_id or secret("TELEGRAM_CHAT_ID")
    if args.send and not chat_id:
        print("--send needs a chat id: pass --chat-id, or set TELEGRAM_CHAT_ID in .env "
              "(run this probe without --send first; it prints the chat ids it can see).",
              file=sys.stderr)
        common.remove_if_empty(outdir)
        return EXIT_MISSING_KEY

    findings = Findings("Telegram probe")
    findings.kv("Run started (UTC)", common.iso(now_utc()))
    findings.kv("Run started (Europe/London)", london(now_utc()))
    planned = 5 if args.send else 2
    budget = RequestBudget(args.max_requests)
    budget.check(planned, "getMe, getUpdates" + (", sendMessage, 2 x editMessageText" if args.send else ""))

    rc = EXIT_OK
    try:
        with common.http_client() as client:
            # 1. getMe -------------------------------------------------------
            findings.section("getMe")
            me = call(client, token, "getMe", None, budget=budget, findings=findings,
                      outdir=outdir, stem="getMe",
                      body_redactor=lambda d: {"ok": True, "id": d["result"].get("id"),
                                               "username": d["result"].get("username"),
                                               "is_bot": d["result"].get("is_bot")})
            bot = me["result"]
            findings.kv("Bot username", f"@{bot.get('username')}" if bot.get("username") else None)
            findings.kv("Bot id", bot.get("id"))
            findings.kv("Bot can join groups", bot.get("can_join_groups"))
            findings.kv("Bot can read all group messages", bot.get("can_read_all_group_messages"))

            # 2. getUpdates --------------------------------------------------
            findings.section("getUpdates: chats seen")
            try:
                upd = call(client, token, "getUpdates", {"limit": 100, "timeout": 0},
                           budget=budget, findings=findings, outdir=outdir, stem="getUpdates",
                           body_redactor=lambda d: {"ok": True, "updates": redact_updates(d["result"])})
            except TelegramError as exc:
                if "409" in str(exc) or "webhook" in str(exc).lower():
                    findings.add(f"{exc}")
                    findings.add("A webhook is set on this bot, so getUpdates is refused. "
                                 "Remove it with the deleteWebhook method (or ask me to) and run again.")
                    upd = {"result": []}
                else:
                    raise
            updates = upd.get("result") or []
            chats = distinct_chats(updates)
            groups = [c for c in chats if c.get("type") != "private"]
            private_ids = [c["id"] for c in chats if c.get("type") == "private"]
            findings.kv("Updates returned", len(updates))
            findings.kv("Distinct chats", len(chats))
            findings.table([[c["id"], c["type"], c.get("title", "")] for c in groups],
                           header=["chat id", "type", "title"])
            findings.kv("Private chats (ids shown on screen only, never saved)", len(private_ids))
            if private_ids:
                # A private chat's id is that person's Telegram user id (rule 2):
                # plain print, so it reaches the screen and no file.
                print("Private chat id(s): " + ", ".join(str(i) for i in private_ids)
                      + "   <- shown here only, not written to findings.md or any other file", flush=True)
            if not chats:
                findings.add("No chats yet. Send the bot any message (or add it to the group and post "
                             "something), then run this probe again. Telegram only keeps updates for "
                             "24 hours and drops them once a webhook is set.")
            else:
                findings.add("Copy the chat id you want alerts in into .env as TELEGRAM_CHAT_ID. A group is "
                             "the better choice: everyone on shift sees the alerts and its id is nobody's "
                             "user id. Group ids are negative; supergroups start with -100.")
            findings.add("Saved copy: getUpdates.body.redacted.json (update ids, dates, chat types, group ids "
                         "and titles, text lengths; no names and no private-chat ids).")

            # 3. --send ------------------------------------------------------
            if args.send:
                findings.section("Sample alert")
                findings.kv("Chat id", chat_id_for_files(chat_id))
                text1 = sample_alert_text(now_utc().astimezone(common.LONDON))
                markup = sample_buttons()
                sent = call(client, token, "sendMessage", {
                    "chat_id": chat_id, "text": text1, "parse_mode": "HTML",
                    "disable_web_page_preview": True, "reply_markup": markup,
                }, budget=budget, findings=findings, outdir=outdir, stem="sendMessage",
                   body_redactor=redact_sent_message)
                message_id = sent["result"]["message_id"]
                findings.kv("message_id", message_id)
                findings.add("Sent text:")
                findings.add("```\n" + text1 + "\n```")

                time.sleep(PAUSE_BETWEEN_EDITS_S)
                text2 = replace_last_line(text1, "TomTom · Waze (3 👍) · lane closed on signs")
                call(client, token, "editMessageText", {
                    "chat_id": chat_id, "message_id": message_id, "text": text2,
                    "parse_mode": "HTML", "reply_markup": markup,
                }, budget=budget, findings=findings, outdir=outdir, stem="edit1",
                   body_redactor=redact_sent_message)
                findings.add("Edit 1: last line is now 'TomTom · Waze (3 👍) · lane closed on signs'")

                time.sleep(PAUSE_BETWEEN_EDITS_S)
                cleared = london(now_utc())
                text3 = text2 + f"\n✅ Cleared {cleared}, live for 27 min"
                call(client, token, "editMessageText", {
                    "chat_id": chat_id, "message_id": message_id, "text": text3,
                    "parse_mode": "HTML", "reply_markup": markup,
                }, budget=budget, findings=findings, outdir=outdir, stem="edit2",
                   body_redactor=redact_sent_message)
                findings.add(f"Edit 2: appended '✅ Cleared {cleared}, live for 27 min'")
                findings.add("Look at the chat: one message, edited twice, with Google Maps and Waze buttons. "
                             "The times are sample values.")
    except TelegramError as exc:
        findings.add(f"ERROR: {exc}")
        if exc.hint:
            findings.add(exc.hint)
        rc = EXIT_FAILED
    except common.httpx.HTTPError as exc:
        findings.add(f"ERROR: request failed: {explain_exception(exc)}")
        rc = EXIT_FAILED

    findings.section("Requests")
    findings.kv("Requests sent", budget.totals())
    findings.write(outdir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
