from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class RemediationSession(BaseModel):
    session_id: str
    issue_number: int
    issue_title: str
    repo_full_name: str
    devin_status: Optional[str] = None
    pr_url: Optional[str] = None
    devin_session_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class Metrics(BaseModel):
    total: int
    by_status: dict
    success_rate: float
    prs_created: int
