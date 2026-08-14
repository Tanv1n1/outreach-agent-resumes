"""
Verifies a drafted message before it's shown as "ready." Two layers:

1. Rule-based checks (instant, free, zero LLM calls) -- catches the
   obvious, mechanical tells: leftover placeholders, common AI-cliche
   phrases, overused em-dashes. These are deterministic and don't need
   a model's judgment to catch.

2. LLM-as-judge (a separate call, deliberately framed as a critic, not
   the same call that wrote the draft) -- catches the subtler stuff a
   regex can't: does this sound templated even without a smoking-gun
   phrase, is the personalization actually specific or just vaguely
   plausible, does the ask feel natural.

Used by drafter.py's generate_verified_message() loop -- this module
only judges, it doesn't rewrite anything itself.
"""

import re
import logging
from groq import Groq
from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)


class VerificationResult(BaseModel):
    sounds_human: bool
    is_specific: bool
    issues: list[str] = Field(default_factory=list)

    @property
    def passes(self) -> bool:
        return self.sounds_human and self.is_specific and not self.issues


# Phrases that are strong tells of AI-generated cold outreach -- caught
# instantly without spending an LLM call. Not exhaustive, but these are
# the recurring, high-confidence offenders.
_CLICHE_PATTERNS = [
    r"\bI'?m excited about\b", r"\bI would love the opportunity\b",
    r"\bin today'?s fast-paced\b", r"\bseamlessly\b", r"\bdelve\b",
    r"\bdive into\b", r"\bfurthermore\b", r"\bmoreover\b", r"\brobust\b",
    r"\bcutting-edge\b", r"\bsynergy\b", r"\bunlock\b", r"\bgame-changer\b",
    r"\bI hope this (email|message) finds you well\b",
    r"\bleverage\b", r"\bat the intersection of\b",
    r"\bpassionate about\b",
]
_PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][^\]]{0,40}\]")   # [Name], [Hiring Manager], etc.


def rule_based_check(body: str) -> list[str]:
    issues = []

    placeholders = _PLACEHOLDER_RE.findall(body)
    if placeholders:
        issues.append(f"Contains unfilled placeholder(s): {', '.join(placeholders)}")

    for pattern in _CLICHE_PATTERNS:
        if re.search(pattern, body, re.IGNORECASE):
            match = re.search(pattern, body, re.IGNORECASE).group(0)
            issues.append(f"AI-cliche phrase: \"{match}\"")

    em_dash_count = body.count("—") + body.count("--")
    if em_dash_count > 2:
        issues.append(f"Overuses em-dashes ({em_dash_count} instances) -- reads as AI-patterned")

    return issues


def llm_verify(profile_summary: str, lead_summary: str, body: str) -> VerificationResult:
    client = Groq(api_key=settings.GROQ_API_KEY, timeout=30.0)

    system_prompt = """You are a skeptical human hiring manager reviewing a cold
outreach message you just received. Judge it as a strict critic, not as
its author.

Answer honestly:
- sounds_human: would a real person reading this suspect it was AI-written?
  Look for: generic enthusiasm without specifics, overly polished/uniform
  sentence rhythm, corporate buzzword density, a "resume summary" feel
  instead of a genuine note. If in doubt, say NO (be strict).
- is_specific: does it reference something concretely real about the
  candidate's actual work (not just skill-name-dropping) AND something
  concretely real about the specific role/company (not generic praise)?
- issues: list anything that would make a real hiring manager roll their
  eyes or immediately recognize this as a template.

Return ONLY valid JSON: {"sounds_human": bool, "is_specific": bool, "issues": [string]}
"""
    user_content = f"CANDIDATE CONTEXT:\n{profile_summary}\n\nROLE:\n{lead_summary}\n\nMESSAGE TO JUDGE:\n{body}"

    response = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        temperature=0.2,   # judge should be fairly consistent, unlike the drafter
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return VerificationResult.model_validate_json(response.choices[0].message.content)