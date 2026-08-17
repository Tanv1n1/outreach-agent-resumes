"""
Scores a JobLead against a CandidateProfile. Deliberately NOT using
embeddings/ML here -- a weighted keyword-overlap score is transparent
(you can see exactly why something scored 72 vs 40), costs nothing to
run, and is good enough for a first-pass filter before human approval.
If matching quality becomes the bottleneck later, this is the function
to swap for an embedding-similarity approach -- the ScoredLead contract
(score + matched_skills + reasons) stays the same either way.

Scoring mode is "broad" per user decision: skill/domain overlap counts
for more than exact title match, so a Product Ops or Program Manager
posting can still score well if the actual work overlaps (LLM automation,
ERP integration, B2B SaaS), not just an exact "Product Manager" title hit.
"""

import re
from core.models import CandidateProfile, JobLead, ScoredLead
from services.lead_finder.experience_matcher import score_experience_match

# Weights sum to 100 -- skills still dominate, but experience is now a
# real factor instead of being ignored entirely (see experience_matcher.py
# module docstring for why this was added: a 2-year candidate and an
# 8-years-required Staff role used to score identically as long as
# skill/domain words overlapped, which is exactly backwards).
WEIGHT_SKILLS = 40
WEIGHT_TITLE = 15
WEIGHT_DOMAIN = 20
WEIGHT_EXPERIENCE = 25

# Adjacent titles that should still count as a strong title-relevance hit
# even if not an exact match against titles_held. Extend as needed --
# this is what makes "broad" matching actually broad instead of just
# falling back to pure keyword soup.
ADJACENT_TITLE_GROUPS = [
    {"product manager", "associate product manager", "product owner", "apm",
     "product operations", "product ops", "technical product manager",
     "program manager", "business analyst", "product analyst"},
]


def score_lead(profile: CandidateProfile, lead: JobLead) -> ScoredLead:
    text = f"{lead.title} {lead.description}".lower()

    skill_score, matched_skills = _score_skills(profile, text)
    title_score, title_reason = _score_title(profile, lead.title)
    domain_score, domain_reason = _score_domain(profile, text)
    experience_score, experience_reason = _score_experience(profile, lead)

    total = skill_score + title_score + domain_score + experience_score

    reasons = []
    if matched_skills:
        reasons.append(f"Matches {len(matched_skills)} of your skills: "
                        f"{', '.join(matched_skills[:5])}" +
                        (f" +{len(matched_skills) - 5} more" if len(matched_skills) > 5 else ""))
    if title_reason:
        reasons.append(title_reason)
    if domain_reason:
        reasons.append(domain_reason)
    reasons.append(experience_reason)
    if not reasons:
        reasons.append("Weak match -- low skill/title/domain overlap")

    return ScoredLead(
        lead=lead,
        score=round(total, 1),
        matched_skills=matched_skills,
        reasons=reasons,
        candidate_id=profile.candidate_id,
    )


def _score_experience(profile: CandidateProfile, lead: JobLead) -> tuple[float, str]:
    exp_ratio, reason = score_experience_match(
        profile.total_experience_years, lead.title, lead.description
    )
    return WEIGHT_EXPERIENCE * exp_ratio, reason


def _score_skills(profile: CandidateProfile, text: str) -> tuple[float, list[str]]:
    if not profile.skills:
        return 0.0, []

    matched = [s for s in profile.skills if _contains_skill(text, s)]
    ratio = len(matched) / len(profile.skills)
    # Cap the effective ratio contribution -- matching 8/40 skills is still
    # a meaningful signal, don't let the denominator punish broad profiles.
    effective_ratio = min(ratio * 3, 1.0)
    return WEIGHT_SKILLS * effective_ratio, matched


def _contains_skill(text: str, skill: str) -> bool:
    # Word-boundary match so "SQL" doesn't match inside "MySQLish" etc.
    # Short/ambiguous skills (<=2 chars) skipped to avoid noise matches.
    skill = skill.strip()
    if len(skill) <= 2:
        return False
    pattern = r"\b" + re.escape(skill.lower()) + r"\b"
    return re.search(pattern, text) is not None


def _score_title(profile: CandidateProfile, job_title: str) -> tuple[float, str]:
    job_title_lower = job_title.lower()
    candidate_titles = [t.lower() for t in profile.titles_held]

    # exact/substring match against something the candidate has actually held
    for t in candidate_titles:
        if t in job_title_lower or job_title_lower in t:
            return WEIGHT_TITLE, f"Title matches your experience as '{t.title()}'"

    # adjacent-title group match -- this is what makes scoring "broad"
    for group in ADJACENT_TITLE_GROUPS:
        job_in_group = any(g in job_title_lower for g in group)
        candidate_in_group = any(any(g in ct for g in group) for ct in candidate_titles)
        if job_in_group and candidate_in_group:
            return WEIGHT_TITLE * 0.7, "Adjacent role to your title -- worth a look"

    return 0.0, ""


def _score_domain(profile: CandidateProfile, text: str) -> tuple[float, str]:
    # Domain terms pulled from work_history/project descriptions -- these
    # are the "what you actually did" signals that a title alone misses
    # (e.g. "ERP integration", "B2B SaaS", "LLM automation").
    domain_terms = set()
    for w in profile.work_history:
        domain_terms.update(_extract_domain_terms(w.description))
    for p in profile.projects:
        domain_terms.update(_extract_domain_terms(p.description))

    if not domain_terms:
        return 0.0, ""

    matched = [d for d in domain_terms if d in text]
    if not matched:
        return 0.0, ""

    ratio = min(len(matched) / max(len(domain_terms), 1) * 4, 1.0)
    return WEIGHT_DOMAIN * ratio, f"Domain overlap: {', '.join(matched[:3])}"


# Common domain phrases worth checking for -- deliberately small/curated
# rather than NLP-extracted, since noisy phrase extraction from free text
# would hurt more than a short curated list helps.
_DOMAIN_VOCAB = [
    "b2b saas", "b2b", "saas", "erp", "procurement", "accounts payable",
    "llm", "ai agent", "automation", "fintech", "payments", "api integration",
    "product roadmap", "stakeholder management", "cross-functional",
]


def _extract_domain_terms(text: str) -> set[str]:
    text_lower = text.lower()
    return {term for term in _DOMAIN_VOCAB if term in text_lower}