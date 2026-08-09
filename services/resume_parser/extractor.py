"""
Turns raw resume text into a structured CandidateProfile using Groq's
hosted inference API.

Model: openai/gpt-oss-120b by default -- Groq's current top general-purpose
model for reasoning/extraction quality (their Llama 3.x/4 chat models were
deprecated in 2026; gpt-oss-120b is the recommended replacement). Falls
back to openai/gpt-oss-20b on error -- smaller, still solid, much faster.

Uses JSON object mode + an explicit schema description in the prompt,
not Groq's strict json_schema mode -- strict mode requires every field
to be listed as "required" with additionalProperties:false, which
fights against genuinely optional fields like `phone` or `location`
that are legitimately absent on many resumes. We validate with Pydantic
after the call instead, and retry via the fallback model on failure --
same pattern as the schema-adherence problem this project already
handles elsewhere.
"""

import logging
import re
from datetime import date
from groq import Groq
from pydantic import BaseModel, Field

from core.models import CandidateProfile, WorkExperience, Project
from config import settings

logger = logging.getLogger(__name__)


# --- Schema describing the LLM I/O contract. Mirrors CandidateProfile but
# kept separate: this is what the model is told to produce, CandidateProfile
# is our internal domain model. Letting them drift independently is
# intentional -- e.g. raw_resume_text and parse_warnings are filled in by
# our own code, not the model. ---

class WorkExperienceSchema(BaseModel):
    company: str
    title: str
    start_date: str
    end_date: str
    description: str = ""
    tech_used: list[str] = Field(default_factory=list)


class ProjectSchema(BaseModel):
    name: str
    description: str = ""
    tech_used: list[str] = Field(default_factory=list)
    date: str = ""


class ResumeSchema(BaseModel):
    full_name: str
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    total_experience_years: float = 0.0
    skills: list[str] = Field(default_factory=list)
    titles_held: list[str] = Field(default_factory=list)
    work_history: list[WorkExperienceSchema] = Field(default_factory=list)
    projects: list[ProjectSchema] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)


def _build_system_prompt() -> str:
    # Injected fresh per call, not a module constant -- an LLM has no
    # built-in notion of "today" (it'll fall back to its training-cutoff
    # date), which breaks "Present" duration math for any currently-held
    # role. Confirmed bug during local-model testing: a role starting
    # July 2024 came back as 0.9 years instead of ~2 years because the
    # model assumed "now" was sometime in early 2025.
    today = date.today().isoformat()
    return f"""You extract structured data from resumes into the given schema.
Today's date is {today}. Use this as "now" for any role marked "Present" --
compute total_experience_years using this real date, not an assumed one.

Rules:
- total_experience_years: sum actual full-time work durations, not internships
  unless no full-time experience exists. Round to 1 decimal.
- skills: build this from the WHOLE resume, not just a "Skills" section if one
  exists. Read each work_history description and project bullet -- if someone
  says "built a real-time pipeline with Kafka and Redis" or "led migration to
  Kubernetes", those are skills even if never listed under a "Skills" heading.
  Combine explicitly-listed skills with skills demonstrably used in their
  actual work. Dedupe, normalize casing (e.g. "REACT.JS" -> "React.js").
  Technical skills only -- not soft skills like "communication".
  IMPORTANT: if the resume has an explicit "Skills" section, you MUST include
  every item listed there -- never leave the skills array empty or sparse
  when a skills section exists in the source text.
- If a field is genuinely not present in the resume, leave it null/empty.
  Do not invent data -- only infer a skill if the resume text actually
  describes using it, not from the job title alone.
- end_date: use the literal string "Present" if the role is current.
- projects: extract personal/side projects as SEPARATE entries from
  work_history -- things under headings like "Personal Projects", "Side
  Projects", "Projects" that are not paid employment. These count as real
  signal (self-built, shipped, used by real users) even if unpaid -- do
  not fold them into work_history and do not skip them.

Return ONLY a single valid JSON object with exactly these top-level keys,
nothing else -- no markdown fences, no preamble, no trailing commentary:
full_name (string), email (string or null), phone (string or null),
location (string or null), total_experience_years (number),
skills (array of strings), titles_held (array of strings),
work_history (array of objects with: company, title, start_date, end_date,
description, tech_used), projects (array of objects with: name,
description, tech_used, date), education (array of strings).
"""


def extract_profile(raw_text: str, candidate_id: str, resume_file_path: str) -> CandidateProfile:
    client = Groq(api_key=settings.GROQ_API_KEY)
    model = settings.GROQ_MODEL

    logger.info("Calling Groq model '%s' for extraction...", model)
    try:
        data = _run_extraction(client, model, raw_text)
        logger.info("Extraction succeeded with '%s'", model)
    except Exception as e:
        logger.warning("Primary model %s failed (%s), trying fallback %s",
                        model, e, settings.GROQ_FALLBACK_MODEL)
        data = _run_extraction(client, settings.GROQ_FALLBACK_MODEL, raw_text)
        logger.info("Extraction succeeded with fallback '%s'", settings.GROQ_FALLBACK_MODEL)

    # Safety net: models sometimes leave `skills` sparse/empty even
    # when a resume has a literal, labeled "Skills" section -- confirmed
    # in earlier testing (model wrote a long work_history description and
    # left skills at []). This is a deterministic regex pass that doesn't
    # rely on model compliance at all, merged in rather than trusted alone.
    regex_skills = _extract_skills_section(raw_text)
    if regex_skills:
        merged = list(data.skills)
        existing_lower = {s.lower() for s in merged}
        for s in regex_skills:
            if s.lower() not in existing_lower:
                merged.append(s)
                existing_lower.add(s.lower())
        if len(merged) > len(data.skills):
            logger.info(
                "Regex skills-section fallback added %d skills the model missed",
                len(merged) - len(data.skills),
            )
        data.skills = merged

    warnings = _validate(data)
    logger.info(
        "Parsed: %d skills, %d work entries, %d projects, %.1f yrs experience",
        len(data.skills), len(data.work_history), len(data.projects),
        data.total_experience_years,
    )
    if warnings:
        logger.warning("Validation warnings: %s", warnings)

    work_history = [
        WorkExperience(
            company=w.company, title=w.title, start_date=w.start_date,
            end_date=w.end_date, description=w.description, tech_used=w.tech_used,
        )
        for w in data.work_history
    ]

    projects = [
        Project(
            name=p.name, description=p.description,
            tech_used=p.tech_used, date=p.date,
        )
        for p in data.projects
    ]

    return CandidateProfile(
        candidate_id=candidate_id,
        full_name=data.full_name or "Unknown",
        email=data.email or "",
        phone=data.phone,
        location=data.location,
        total_experience_years=data.total_experience_years,
        skills=data.skills,
        titles_held=data.titles_held,
        work_history=work_history,
        projects=projects,
        education=data.education,
        raw_resume_text=raw_text,
        resume_file_path=resume_file_path,
        parse_warnings=warnings,
    )


def _run_extraction(client: Groq, model: str, raw_text: str) -> ResumeSchema:
    response = client.chat.completions.create(
        model=model,
        temperature=0,   # deterministic -- same resume should parse the same way twice
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": raw_text},
        ],
    )
    raw_json = response.choices[0].message.content
    return ResumeSchema.model_validate_json(raw_json)


# Matches a "SKILLS" (or "TECHNICAL SKILLS", "SKILLS & TOOLS", etc.) section
# header on its own line, capturing everything up to the next all-caps
# section header (EXPERIENCE, EDUCATION, PROJECTS, ...) or end of text.
_SKILLS_SECTION_RE = re.compile(
    r"^\s*[A-Z0-9 &/]*\bSKILLS\b[A-Z0-9 &/]*\s*$\n(.*?)(?=^\s*[A-Z][A-Z0-9 &/]{2,}\s*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
_BULLET_PREFIX_RE = re.compile(r"^[\u2022\-\*]\s*")


def _extract_skills_section(raw_text: str) -> list[str]:
    """Deterministic fallback for when the LLM under-extracts an explicit
    Skills section. Handles the common "• Category: item · item · item"
    bullet style as well as plain comma-separated lists."""
    match = _SKILLS_SECTION_RE.search(raw_text)
    if not match:
        return []

    section = match.group(1)
    items: list[str] = []
    for line in section.splitlines():
        line = _BULLET_PREFIX_RE.sub("", line.strip())
        if not line:
            continue
        # drop a leading category label like "Product:" or "AI / Agents:"
        if ":" in line[:40]:
            line = line.split(":", 1)[1]
        # split on the interpunct (·) commonly used as a delimiter, or comma
        parts = re.split(r"[·,]", line)
        for p in parts:
            p = p.strip(" .")
            if p and len(p) < 60:  # guard against swallowing a stray sentence
                items.append(p)
    return items


def _validate(data: ResumeSchema) -> list[str]:
    """Non-fatal sanity checks surfaced to the user via Telegram rather
    than blocking the pipeline -- see parser.py for how these are used."""
    warnings = []
    if not data.email:
        warnings.append("No email detected -- required before outreach can start.")
    if not data.skills:
        warnings.append("No skills detected -- lead matching will be unreliable.")
    if not data.work_history:
        warnings.append("No work history detected -- check if resume parsed correctly.")
    if data.total_experience_years < 0 or data.total_experience_years > 50:
        warnings.append(
            f"total_experience_years looks off ({data.total_experience_years}) -- verify manually."
        )
    return warnings


class ExtractionError(Exception):
    pass