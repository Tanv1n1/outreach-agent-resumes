"""
Drafts a personalized outreach message for one APPROVED lead, using the
candidate's real profile (skills, work history, projects) as the actual
source of personalization -- not a fill-in-the-blank template. The draft
is shown back to the user for review before anything is considered
ready to send (see telegram_bot.py's approve-to-send flow); this module
never sends anything itself.

Channel detection: LinkedIn URLs get a short LinkedIn-style message (no
subject line, tighter length -- InMail/connection-note conventions are
different from email). Everything else defaults to an email draft with
a subject line.
"""

import json
import logging
import re

from groq import Groq
from pydantic import BaseModel

from core.models import CandidateProfile
from config import settings

logger = logging.getLogger(__name__)


class MessageDraftSchema(BaseModel):
    subject: str | None = None
    body: str


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    # Greenhouse job descriptions come back as raw HTML -- strip tags for
    # a cleaner prompt input. Not trying to preserve formatting, just
    # giving the model readable text to draw context from.
    return _HTML_TAG_RE.sub(" ", text or "").strip()


def _detect_channel(lead: dict) -> str:
    url = (lead.get("url") or "").lower()
    return "linkedin" if "linkedin.com" in url else "email"


def _build_profile_context(profile: CandidateProfile) -> str:
    # Deliberately compact -- the model doesn't need the entire raw resume,
    # just enough real, specific material to personalize with. Current
    # role + most relevant project cover most of what makes a message
    # feel individually written rather than templated.
    current_role = ""
    if profile.work_history:
        w = profile.work_history[0]
        current_role = f"Current role: {w.title} at {w.company} -- {w.description[:400]}"

    project_summary = ""
    if profile.projects:
        p = profile.projects[0]
        project_summary = f"Notable project: {p.name} -- {p.description[:300]}"

    return (
        f"Name: {profile.full_name}\n"
        f"Total experience: {profile.total_experience_years} years\n"
        f"Skills: {', '.join(profile.skills[:20])}\n"
        f"{current_role}\n"
        f"{project_summary}\n"
        f"Contact: {profile.email}" + (f" | {profile.phone}" if profile.phone else "")
    )


def _build_system_prompt(channel: str) -> str:
    length_rule = (
        "Keep the body under 120 words -- LinkedIn messages to non-connections "
        "get ignored past a couple short paragraphs. No subject line needed."
        if channel == "linkedin" else
        "Keep the body under 180 words. Include a short, specific subject line."
    )
    return f"""You write a cold outreach message from a job candidate to an HR
contact or hiring manager, on the candidate's behalf.

Rules:
- Reference the SPECIFIC role and company by name -- never generic ("a role at your company").
- Personalize using 1-2 concrete things from the candidate's actual background
  (a real skill, project, or achievement) that genuinely connects to what the
  role needs -- not a generic list of skills copy-pasted in.
- Confident, professional tone. Not desperate, not overly formal, not salesy.
- End with a clear, low-friction ask (e.g. "would love a quick chat if there's
  a fit" -- not a hard sell).
- Sign off with the candidate's actual name.
- {length_rule}
- Write in first person, as the candidate.
- Do NOT invent achievements, numbers, or experience not present in the
  candidate context given to you.

Return ONLY a valid JSON object: {{"subject": string or null, "body": string}}.
No markdown fences, no commentary.
"""


def generate_message(profile: CandidateProfile, lead: dict) -> dict:
    channel = _detect_channel(lead)
    # timeout set explicitly -- an unbounded hang here would look
    # identical to total silence in Telegram (no error, nothing happens),
    # exactly the failure mode being debugged. Bounding it turns a silent
    # hang into a real, visible TimeoutError after 30s.
    client = Groq(api_key=settings.GROQ_API_KEY, timeout=30.0)

    matched_skills = json.loads(lead.get("matched_skills") or "[]")
    lead_context = (
        f"Role: {lead['title']}\n"
        f"Company: {lead['company']}\n"
        f"Location: {lead.get('location') or 'n/a'}\n"
        f"Matched skills from candidate: {', '.join(matched_skills) if matched_skills else 'n/a'}\n"
        f"Job description (may be partial): {_strip_html(lead.get('description', ''))[:1500]}"
    )

    user_content = f"CANDIDATE:\n{_build_profile_context(profile)}\n\nROLE:\n{lead_context}"

    model = settings.GROQ_MODEL
    try:
        data = _run_draft(client, model, channel, user_content)
    except Exception as e:
        logger.warning("Primary model %s failed (%s), trying fallback", model, e)
        data = _run_draft(client, settings.GROQ_FALLBACK_MODEL, channel, user_content)

    return {"subject": data.subject, "body": data.body, "channel": channel}


def _run_draft(client: Groq, model: str, channel: str, user_content: str) -> MessageDraftSchema:
    response = client.chat.completions.create(
        model=model,
        temperature=0.4,   # a little variance is fine/desirable here, unlike
                            # extraction -- identical wording every regenerate
                            # would defeat the point of a "regenerate" button
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _build_system_prompt(channel)},
            {"role": "user", "content": user_content},
        ],
    )
    return MessageDraftSchema.model_validate_json(response.choices[0].message.content)