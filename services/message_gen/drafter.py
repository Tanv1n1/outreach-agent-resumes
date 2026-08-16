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
- NEVER use a placeholder like "[Hiring Manager Name]" or "[Name]" -- the
  candidate does not know the recipient's name. Open with something that
  doesn't require one: "Hi there," "Hi [Company] team," or just launching
  straight into the first sentence with no greeting line at all.
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
    """Single-shot draft, no verification loop. Kept for callers that
    explicitly want just one attempt (e.g. the Regenerate button, which
    is itself a manual iteration -- looping automatically there would
    fight the user's own judgment about when a draft is good enough)."""
    channel = _detect_channel(lead)
    user_content = _build_user_content(profile, lead)
    return _draft_once(channel, user_content)


def generate_verified_message(profile: CandidateProfile, lead: dict, max_attempts: int = 2) -> dict:
    """The critical-path version: drafts, then runs it through
    rule_based_check + llm_verify (see verifier.py). On failure, the
    specific issues found are fed back into the next attempt as explicit
    correction instructions -- not just "try again", but "try again,
    and specifically fix X". Loops up to max_attempts.

    If it still hasn't passed after max_attempts, returns the last
    attempt anyway with verification_passed=False and the remaining
    issues attached -- a human should still see it and decide, rather
    than the pipeline silently discarding a draft that might be fine
    but tripped an overly cautious judge call.
    """
    from services.message_gen.verifier import rule_based_check, llm_verify

    channel = _detect_channel(lead)
    user_content = _build_user_content(profile, lead)
    profile_summary = _build_profile_context(profile)
    lead_summary = _build_lead_context(lead)

    feedback = None
    result = None
    all_issues: list[str] = []

    for attempt in range(1, max_attempts + 1):
        result = _draft_once(channel, user_content, feedback=feedback)

        rule_issues = rule_based_check(result["body"])
        if rule_issues:
            logger.info("Attempt %d failed rule-based check: %s", attempt, rule_issues)
            all_issues = rule_issues
            feedback = _format_feedback(rule_issues)
            continue

        try:
            verdict = llm_verify(profile_summary, lead_summary, result["body"])
        except Exception as e:
            # Judge call failing shouldn't lose an otherwise-fine draft --
            # log it, treat this attempt as unverified-but-passable, stop here.
            logger.warning("Verifier LLM call failed (%s) -- returning draft unverified", e)
            result["verification_passed"] = None
            result["verification_attempts"] = attempt
            result["verification_issues"] = []
            return result

        if verdict.passes:
            logger.info("Attempt %d passed verification (sounds_human=%s, is_specific=%s)",
                        attempt, verdict.sounds_human, verdict.is_specific)
            result["verification_passed"] = True
            result["verification_attempts"] = attempt
            result["verification_issues"] = []
            return result

        logger.info("Attempt %d failed LLM verification: %s", attempt, verdict.issues)
        all_issues = verdict.issues
        feedback = _format_feedback(verdict.issues)

    # Exhausted attempts -- return the last draft anyway, flagged clearly
    logger.warning("Draft never passed verification after %d attempts: %s", max_attempts, all_issues)
    result["verification_passed"] = False
    result["verification_attempts"] = max_attempts
    result["verification_issues"] = all_issues
    return result


def _format_feedback(issues: list[str]) -> str:
    return (
        "Your previous attempt had these specific problems -- fix them directly, "
        "don't just paraphrase around them:\n" + "\n".join(f"- {i}" for i in issues)
    )


def _build_lead_context(lead: dict) -> str:
    matched_skills = json.loads(lead.get("matched_skills") or "[]")
    return (
        f"Role: {lead['title']}\n"
        f"Company: {lead['company']}\n"
        f"Location: {lead.get('location') or 'n/a'}\n"
        f"Matched skills from candidate: {', '.join(matched_skills) if matched_skills else 'n/a'}\n"
        f"Job description (may be partial): {_strip_html(lead.get('description', ''))[:1500]}"
    )


def _build_user_content(profile: CandidateProfile, lead: dict) -> str:
    return f"CANDIDATE:\n{_build_profile_context(profile)}\n\nROLE:\n{_build_lead_context(lead)}"


def _draft_once(channel: str, user_content: str, feedback: str = None) -> dict:
    client = Groq(api_key=settings.GROQ_API_KEY, timeout=30.0)
    model = settings.GROQ_MODEL

    messages = [
        {"role": "system", "content": _build_system_prompt(channel)},
        {"role": "user", "content": user_content},
    ]
    if feedback:
        messages.append({"role": "user", "content": feedback})

    try:
        data = _run_draft_messages(client, model, messages)
    except Exception as e:
        logger.warning("Primary model %s failed (%s), trying fallback", model, e)
        data = _run_draft_messages(client, settings.GROQ_FALLBACK_MODEL, messages)

    return {"subject": data.subject, "body": data.body, "channel": channel}


def _run_draft_messages(client: Groq, model: str, messages: list) -> MessageDraftSchema:
    response = client.chat.completions.create(
        model=model,
        temperature=0.4,
        response_format={"type": "json_object"},
        messages=messages,
    )
    return MessageDraftSchema.model_validate_json(response.choices[0].message.content)