"""
Telegram bot -- the approval layer from the original spec (step 2-4:
present leads, get a single approve/reject tap, notify on updates).

Unlike every other script in this project, this one is a long-running
process (python run_bot.py), not a run-once CLI tool -- it has to sit
and listen for your button taps.

Flow:
  1. You message the bot /start once -- it captures your chat_id and
     stores it in sqlite (there's no way to know it in advance).
  2. You (or a scheduled job later) call push_new_leads(candidate_id) --
     every 'new' lead gets sent as a message with Approve/Reject buttons.
  3. Tapping a button fires _handle_callback, which updates the lead's
     status in the DB and edits the message to show the decision --
     no dangling "waiting for input" state, the message itself becomes
     the record of what you decided.

Approval here only updates status to 'approved' -- it does NOT send
anything to an HR contact. That's message_gen + dispatch's job (not yet
built). This module's entire responsibility is the human-in-the-loop
decision point, kept deliberately separate from anything that actually
contacts a stranger.
"""

import logging
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from core import db
from config import settings

logger = logging.getLogger(__name__)


def _format_lead_message(lead: dict) -> str:
    reasons = json.loads(lead["reasons"]) if lead["reasons"] else []
    reasons_text = "\n".join(f"• {r}" for r in reasons)
    location = lead["location"] or "n/a"
    return (
        f"<b>{lead['title']}</b>\n"
        f"{lead['company']} — {location}\n"
        f"Score: <b>{lead['score']}</b>  |  Source: {lead['source']}\n\n"
        f"{reasons_text}\n\n"
        f"{lead['url'] or '(no link)'}"
    )


def _lead_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{lead_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{lead_id}"),
    ]])


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.set_telegram_chat_id(chat_id)
    await update.message.reply_text(
        "Connected. I'll send job leads here for your approval.\n\n"
        "Commands:\n"
        "/newleads <candidate_id> — push any leads awaiting approval\n"
        "/pending <candidate_id> — count of leads still waiting on you"
    )
    logger.info("Telegram chat_id registered: %s", chat_id)


async def _newleads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /newleads <candidate_id>")
        return
    candidate_id = context.args[0]
    leads = db.get_leads(candidate_id, status="new")

    if not leads:
        await update.message.reply_text("No new leads waiting right now.")
        return

    await update.message.reply_text(f"Sending {len(leads)} lead(s) for your review...")
    for lead in leads:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_format_lead_message(lead),
            parse_mode="HTML",
            reply_markup=_lead_keyboard(lead["id"]),
            disable_web_page_preview=True,
        )


async def _pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pending <candidate_id>")
        return
    count = len(db.get_leads(context.args[0], status="new"))
    await update.message.reply_text(f"{count} lead(s) awaiting your decision.")


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()   # stops the Telegram client's loading spinner

    action, lead_id_str = query.data.split(":", 1)
    lead_id = int(lead_id_str)
    lead = db.get_lead(lead_id)

    if not lead:
        await query.edit_message_text("This lead no longer exists.")
        return
    if lead["status"] != "new":
        # already decided (e.g. double-tap, or decided from another device) --
        # don't silently no-op, tell the user what's actually stored
        await query.edit_message_text(
            f"{query.message.text}\n\n— Already marked: {lead['status']}",
            parse_mode=None,
        )
        return

    new_status = "approved" if action == "approve" else "rejected"
    db.update_lead_status(lead_id, new_status)

    marker = "✅ APPROVED" if new_status == "approved" else "❌ REJECTED"
    await query.edit_message_text(
        text=f"{_format_lead_message(lead)}\n\n<b>{marker}</b>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.info("Lead %s marked %s", lead_id, new_status)


def push_new_leads_sync(candidate_id: str):
    """Non-bot entry point for pushing leads outside of a Telegram command
    -- e.g. called right after run_lead_finder.py finishes, so you get
    pushed leads automatically instead of having to remember to type
    /newleads yourself. Requires the bot to have been /start'd at least
    once already (chat_id must be on file)."""
    import asyncio
    from telegram import Bot

    chat_id = db.get_telegram_chat_id()
    if not chat_id:
        logger.warning("No Telegram chat_id on file yet -- message the bot /start first")
        return

    leads = db.get_leads(candidate_id, status="new")
    if not leads:
        logger.info("No new leads to push for candidate %s", candidate_id)
        return

    async def _push():
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=f"Found {len(leads)} new lead(s) for review:")
        for lead in leads:
            await bot.send_message(
                chat_id=chat_id,
                text=_format_lead_message(lead),
                parse_mode="HTML",
                reply_markup=_lead_keyboard(lead["id"]),
                disable_web_page_preview=True,
            )

    asyncio.run(_push())
    logger.info("Pushed %d leads to Telegram", len(leads))


def build_app() -> Application:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env -- get one from @BotFather")

    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("newleads", _newleads))
    app.add_handler(CommandHandler("pending", _pending))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    return app