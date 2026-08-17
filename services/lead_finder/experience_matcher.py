"""
Matches a candidate's actual years of experience against what a role
actually needs -- the one factor the original scorer completely ignored.
Without this, "Staff Product Manager" (needs 8+ years) and "Associate
Product Manager" (needs 0-2) score identically as long as skill words
overlap, which is exactly backwards for filtering leads accurately.

Two signals, used together since neither alone is reliable:
1. Explicit years stated in the JD text ("5+ years", "3-5 years
   experience") -- regex-extracted, most reliable when present, but
   many postings never state this explicitly.
2. Seniority implied by the job TITLE itself (Senior/Staff/Principal/
   Director vs Associate/Junior/entry-level) -- always available, less
   precise, used as a fallback and as a sanity-check against #1.

This is inherently heuristic -- free-text job postings don't follow one
format. The goal is "meaningfully better than ignoring experience
entirely," not a perfect classifier.
"""

import re

# (regex pattern, extractor) -- ordered roughly by specificity/reliability.
# All patterns look for a number near the word "year(s)" with experience-
# related context nearby, not just any number in the text.
_RANGE_PATTERNS = [
    re.compile(r"(\d+)\s*[-–to]+\s*(\d+)\+?\s*years?", re.IGNORECASE),      # "3-5 years", "3 to 5 years"
    re.compile(r"(\d+)\+\s*years?", re.IGNORECASE),                          # "5+ years"
    re.compile(r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d+)\s*years?", re.IGNORECASE),
    re.compile(r"(\d+)\s*years?\s*(?:of\s*)?(?:relevant\s*)?experience", re.IGNORECASE),
]


def extract_required_years(description: str) -> tuple[float, float] | None:
    """Returns (min_years, max_years) if a requirement was found in the
    text, else None. When only a single number is found (not a range),
    max is set to min + 3 as a loose upper bound -- postings that say
    "5+ years" rarely mean literally only a 5.0-5.9 year band."""
    if not description:
        return None

    text = description[:3000]   # requirement is almost always stated early; avoids scanning huge HTML dumps

    m = _RANGE_PATTERNS[0].search(text)
    if m:
        return float(m.group(1)), float(m.group(2))

    for pattern in _RANGE_PATTERNS[1:]:
        m = pattern.search(text)
        if m:
            min_y = float(m.group(1))
            return min_y, min_y + 3.0

    return None


# Ordered seniority tiers with an approximate expected experience range.
# Checked in order -- more senior keywords checked first so e.g. "Senior
# Associate" (rare but real) resolves to the more specific/senior match.
_SENIORITY_TIERS = [
    (r"\b(intern|trainee)\b", (0.0, 1.0)),
    (r"\b(director|vp|vice president|head of|chief)\b", (8.0, 20.0)),
    (r"\b(principal|staff|group product manager|gpm)\b", (6.0, 12.0)),
    (r"\bsenior\b", (4.0, 8.0)),
    (r"\b(junior|associate|entry.?level|apm)\b", (0.0, 2.5)),
    # no keyword matched -- "plain" title (e.g. just "Product Manager")
    # treated as mid-level, the most common unstated default
]
_DEFAULT_TIER = (2.0, 5.0)


def infer_seniority_from_title(title: str) -> tuple[float, float]:
    title_lower = title.lower()
    for pattern, tier_range in _SENIORITY_TIERS:
        if re.search(pattern, title_lower):
            return tier_range
    return _DEFAULT_TIER


def score_experience_match(candidate_years: float, job_title: str, description: str) -> tuple[float, str]:
    """Returns (score 0.0-1.0, human-readable reason). Score of 1.0 means
    a good experience fit; drops off sharply when the candidate is
    meaningfully under-qualified (the case that actually matters for
    filtering -- an Associate PM applying to Staff-level roles should
    NOT score well just because the skill keywords match)."""

    stated_range = extract_required_years(description)
    min_req, max_req = stated_range if stated_range else infer_seniority_from_title(job_title)
    source = "stated in posting" if stated_range else "inferred from title"

    if min_req <= candidate_years <= max_req:
        return 1.0, f"Experience fits ({candidate_years}y vs {min_req:.1f}-{max_req:.1f}y required, {source})"

    if candidate_years < min_req:
        gap = min_req - candidate_years
        # Sharp falloff -- a 1-2 year gap is common/survivable (companies
        # often flex down), a 4+ year gap almost never is.
        if gap <= 1.0:
            return 0.7, f"Slightly under the {min_req:.1f}y typically expected ({source}) -- still worth trying"
        elif gap <= 3.0:
            return 0.3, f"Under-qualified: role wants {min_req:.1f}+ years ({source}), you have {candidate_years}y"
        else:
            return 0.05, f"Significant experience gap: role wants {min_req:.1f}+ years ({source}), you have {candidate_years}y"

    # over-qualified -- much less disqualifying than under-qualified, but
    # still worth a small penalty + a visible note rather than pretending
    # it's a perfect fit
    over_by = candidate_years - max_req
    if over_by <= 3.0:
        return 0.85, f"Slightly above the typical range ({source}) -- shouldn't be an issue"
    return 0.6, f"Over-qualified vs typical range for this title ({source}) -- may be a step down"