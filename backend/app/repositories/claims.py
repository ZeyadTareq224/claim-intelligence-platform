"""Short-lived database operations for claim extraction."""
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import Claim, ClaimAnalysis


@dataclass(frozen=True)
class ClaimInput:
    description: str
    has_policy_association: bool


class ClaimNotFound(LookupError):
    pass


class ClaimRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def read_input(self, claim_id: UUID) -> ClaimInput:
        with self.sessions() as session:
            claim = session.get(Claim, claim_id)
            if claim is None:
                raise ClaimNotFound("Claim not found")
            return ClaimInput(claim.description, claim.policy_id is not None)

    def add_analysis(self, analysis: ClaimAnalysis) -> ClaimAnalysis:
        with self.sessions.begin() as session:
            session.add(analysis)
            session.flush()
            # Load the database default before the session closes.
            session.refresh(analysis)
            session.expunge(analysis)
        return analysis
