"""
Shared data models. Kept as plain dataclasses (not pydantic) on purpose --
this project has no web framework in front of it yet, so we don't need
validation-on-parse, just a stable shape every service can import.

If a FastAPI layer gets added later, wrap these instead of replacing them.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json


@dataclass
class WorkExperience:
    company: str
    title: str
    start_date: str          # kept as raw string ("Jan 2022") -- normalized later if needed
    end_date: str             # "Present" is valid
    description: str = ""
    tech_used: list[str] = field(default_factory=list)


@dataclass
class Project:
    """Personal/side projects, distinct from paid work_history. Still a
    real signal for lead_finder scoring -- someone who shipped something
    solo with real users is worth matching on, even with thin employment
    history (e.g. early-career candidates)."""
    name: str
    description: str = ""
    tech_used: list[str] = field(default_factory=list)
    date: str = ""            # kept loose ("2026", "Jan-Mar 2025") -- projects rarely have clean dates


@dataclass
class JobLead:
    """A single job posting or company-fit lead, before scoring.
    source distinguishes where it came from -- matters later for
    dedup logic and for knowing which dispatch path applies (a company
    lead with no posted role needs a colder outreach message than a
    lead sourced from an actual job posting)."""
    source: str                # "adzuna" | "remoteok" | "company_manual" etc.
    title: str
    company: str
    location: str = ""
    description: str = ""
    url: str = ""
    posted_at: str = ""
    external_id: str = ""      # source's own id, used for de-duping re-runs


@dataclass
class ScoredLead:
    """JobLead + score, ready to persist and show to the user for approval."""
    lead: JobLead
    score: float                # 0-100
    matched_skills: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    candidate_id: str = ""
    status: str = "new"         # new | approved | rejected | expired | contacted


@dataclass
class CandidateProfile:
    """
    Output of resume_parser. This is the single source of truth that
    lead_finder scores against and message_gen writes from.
    """
    candidate_id: str
    full_name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None

    total_experience_years: float = 0.0
    skills: list[str] = field(default_factory=list)
    titles_held: list[str] = field(default_factory=list)   # e.g. ["Backend Engineer", "SDE-2"]
    target_roles: list[str] = field(default_factory=list)   # what they want next -- can be inferred or user-set
    work_history: list[WorkExperience] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    education: list[str] = field(default_factory=list)

    raw_resume_text: str = ""      # kept for embedding/scoring, not shown to user
    resume_file_path: str = ""
    parsed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # confidence flags -- lead_finder and the Telegram bot both check these
    # before trusting the profile. Cheap insurance against silent bad parses.
    parse_warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    @staticmethod
    def from_dict(d: dict) -> "CandidateProfile":
        work_history = [WorkExperience(**w) for w in d.get("work_history", [])]
        projects = [Project(**p) for p in d.get("projects", [])]
        d = {**d, "work_history": work_history, "projects": projects}
        return CandidateProfile(**d)
