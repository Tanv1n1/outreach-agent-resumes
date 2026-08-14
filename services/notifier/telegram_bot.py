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
from services.message_gen.drafter import generate_message, generate_verified_message

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


def _draft_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Approve to send", callback_data=f"senddraft:{lead_id}"),
        InlineKeyboardButton("🔄 Regenerate", callback_data=f"redraft:{lead_id}"),
        InlineKeyboardButton("🗑️ Discard", callback_data=f"discard:{lead_id}"),
    ]])


def _format_draft_message(draft: dict) -> str:
    subject_line = f"<b>Subject:</b> {draft['subject']}\n\n" if draft.get("subject") else ""
    status_line = _format_verification_status(draft)
    return f"{subject_line}{draft['body']}{status_line}"


def _format_verification_status(draft: dict) -> str:
    passed = draft.get("verification_passed")
    attempts = draft.get("verification_attempts")
    if passed is True:
        return f"\n\n<i>✓ Verified human-sounding (passed on attempt {attempts})</i>"
    if passed is False:
        issues = draft.get("verification_issues", [])
        issues_text = "; ".join(issues[:2]) if issues else "unspecified"
        return (
            f"\n\n<i>⚠️ Did not fully pass verification after {attempts} attempts "
            f"({issues_text}) -- review carefully before sending.</i>"
        )
    if passed is None:
        return "\n\n<i>⚠️ Verification check itself failed (see logs) -- review carefully.</i>"
    return ""


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()   # stops the Telegram client's loading spinner

    action, lead_id_str = query.data.split(":", 1)
    lead_id = int(lead_id_str)
    lead = db.get_lead(lead_id)

    if not lead:
        await query.edit_message_text("This lead no longer exists.")
        return

    try:
        if action in ("approve", "reject"):
            await _handle_lead_decision(query, context, lead, action)
        elif action in ("senddraft", "redraft", "discard"):
            await _handle_draft_action(query, context, lead, action)
    except Exception as e:
        # python-telegram-bot's own internal error logger doesn't always
        # propagate to our logging config (confirmed during testing --
        # a failure here produced zero console output, not even a
        # traceback). Catching broadly here so a bug is never silent:
        # always both logged AND visible to the user in Telegram.
        logger.exception("Callback handler failed for action=%s lead_id=%s", action, lead_id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Something went wrong processing that: {type(e).__name__}: {e}",
        )


async def _handle_lead_decision(query, context, lead: dict, action: str):
    if lead["status"] != "new":
        await query.edit_message_text(
            f"{query.message.text}\n\n— Already marked: {lead['status']}",
            parse_mode=None,
        )
        return

    new_status = "approved" if action == "approve" else "rejected"
    db.update_lead_status(lead["id"], new_status)

    marker = "✅ APPROVED" if new_status == "approved" else "❌ REJECTED"
    await query.edit_message_text(
        text=f"{_format_lead_message(lead)}\n\n<b>{marker}</b>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.info("Lead %s marked %s", lead["id"], new_status)

    if new_status == "approved":
        await _generate_and_send_draft(query, context, lead)


async def _generate_and_send_draft(query, context, lead: dict):
    from core.models import CandidateProfile
    profile_dict = db.get_profile(lead["candidate_id"])
    if not profile_dict:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Couldn't draft a message -- candidate profile not found.",
        )
        return

    profile = CandidateProfile.from_dict(profile_dict)
    await context.bot.send_message(chat_id=query.message.chat_id, text="Drafting a message for this one... (this includes a verification pass, may take ~15-30s)")

    try:
        draft = generate_verified_message(profile, lead)
    except Exception as e:
        logger.exception("Draft generation failed for lead %s", lead["id"])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Draft generation failed: {e}",
        )
        return

    db.save_draft(lead["id"], lead["candidate_id"], draft["subject"], draft["body"], draft["channel"])
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=_format_draft_message(draft),
        parse_mode="HTML",
        reply_markup=_draft_keyboard(lead["id"]),
    )


async def _handle_draft_action(query, context, lead: dict, action: str):
    if action == "discard":
        db.update_draft_status(lead["id"], "discarded")
        await query.edit_message_text(f"{query.message.text}\n\n<b>🗑️ DISCARDED</b>", parse_mode="HTML")
        return

    if action == "senddraft":
        # Marks intent only -- actual sending is dispatch's job (not yet
        # built). This deliberately stops short of contacting anyone;
        # it's the last human checkpoint before that module exists.
        db.update_draft_status(lead["id"], "approved_to_send")
        await query.edit_message_text(
            f"{query.message.text}\n\n<b>📤 APPROVED TO SEND</b>\n"
            f"(Sending isn't wired up yet -- this is queued for when dispatch is built.)",
            parse_mode="HTML",
        )
        return

    if action == "redraft":
        from core.models import CandidateProfile
        profile_dict = db.get_profile(lead["candidate_id"])
        profile = CandidateProfile.from_dict(profile_dict)
        await query.edit_message_text(f"{query.message.text}\n\n<i>Regenerating...</i>", parse_mode="HTML")
        try:
            draft = generate_message(profile, lead)
        except Exception as e:
            logger.exception("Redraft failed for lead %s", lead["id"])
            await query.edit_message_text(f"Redraft failed: {e}")
            return
        db.save_draft(lead["id"], lead["candidate_id"], draft["subject"], draft["body"], draft["channel"])
        await query.edit_message_text(
            text=_format_draft_message(draft),
            parse_mode="HTML",
            reply_markup=_draft_keyboard(lead["id"]),
        )


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