from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PolicyType(str, enum.Enum):
    MOTOR_COMPREHENSIVE = "motor_comprehensive"
    MOTOR_THIRD_PARTY_LIABILITY = "motor_third_party_liability"


class PolicyStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ClauseType(str, enum.Enum):
    COVERAGE = "coverage"
    EXCLUSION = "exclusion"
    CONDITION = "condition"
    DEDUCTIBLE = "deductible"


class ClaimStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    CLOSED = "closed"


class ProcessingStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisStatus(str, enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"


def enum_type(cls):
    return Enum(cls, values_callable=lambda items: [x.value for x in items], native_enum=False, create_constraint=True, name=cls.__name__.lower())


def identity():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def timestamp():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (CheckConstraint("name ~ '[^[:space:]]'", name="ck_org_name"), CheckConstraint("code ~ '[^[:space:]]'", name="ck_org_code"))
    id: Mapped[uuid.UUID] = identity()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    created_at: Mapped[datetime] = timestamp()
    vehicles: Mapped[list[Vehicle]] = relationship(back_populates="organization")
    policies: Mapped[list[Policy]] = relationship(back_populates="organization", overlaps="vehicle,policies")
    claims: Mapped[list[Claim]] = relationship(back_populates="organization", overlaps="policy,claims")


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (UniqueConstraint("organization_id", "id", name="uq_vehicle_org_id"), UniqueConstraint("organization_id", "vin", name="uq_vehicle_org_vin"), CheckConstraint("manufacture_year >= 1886 AND manufacture_year <= 2100", name="ck_vehicle_year"))
    id: Mapped[uuid.UUID] = identity()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    vin: Mapped[str | None] = mapped_column(String(17))
    plate_number: Mapped[str] = mapped_column(String(40), nullable=False)
    make: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    manufacture_year: Mapped[int] = mapped_column(Integer, nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="vehicles")
    policies: Mapped[list[Policy]] = relationship(back_populates="vehicle", overlaps="organization,policies")


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("organization_id", "id", name="uq_policy_org_id"), UniqueConstraint("organization_id", "policy_number", name="uq_policy_org_number"), ForeignKeyConstraint(["organization_id", "vehicle_id"], ["vehicles.organization_id", "vehicles.id"], name="fk_policy_vehicle_org"), CheckConstraint("expiration_date > effective_date", name="ck_policy_dates"), CheckConstraint("sum_insured >= 0", name="ck_policy_sum"), CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_policy_currency"))
    id: Mapped[uuid.UUID] = identity()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    policy_number: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_type: Mapped[PolicyType] = mapped_column(enum_type(PolicyType), nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(enum_type(PolicyStatus), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiration_date: Mapped[date] = mapped_column(Date, nullable=False)
    sum_insured: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = timestamp()
    organization: Mapped[Organization] = relationship(back_populates="policies", overlaps="vehicle,policies")
    vehicle: Mapped[Vehicle] = relationship(back_populates="policies", overlaps="organization,policies")
    clauses: Mapped[list[PolicyClause]] = relationship(back_populates="policy")
    documents: Mapped[list[PolicyDocument]] = relationship(back_populates="policy")
    claims: Mapped[list[Claim]] = relationship(back_populates="policy", overlaps="organization,claims")


class PolicyClause(Base):
    __tablename__ = "policy_clauses"
    __table_args__ = (UniqueConstraint("policy_id", "clause_code", name="uq_clause_code"), CheckConstraint("content ~ '[^[:space:]]'", name="ck_clause_content"), CheckConstraint("limit_amount IS NULL OR limit_amount >= 0", name="ck_clause_limit"), CheckConstraint("deductible_amount IS NULL OR deductible_amount >= 0", name="ck_clause_deductible"))
    id: Mapped[uuid.UUID] = identity()
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id"), nullable=False, index=True)
    clause_code: Mapped[str] = mapped_column(String(80), nullable=False)
    clause_type: Mapped[ClauseType] = mapped_column(enum_type(ClauseType), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    limit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    deductible_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    policy: Mapped[Policy] = relationship(back_populates="clauses")


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("organization_id", "claim_number", name="uq_claim_org_number"), ForeignKeyConstraint(["organization_id", "policy_id"], ["policies.organization_id", "policies.id"], name="fk_claim_policy_org"), CheckConstraint("description ~ '[^[:space:]]'", name="ck_claim_description"), CheckConstraint("claimed_amount IS NULL OR claimed_amount >= 0", name="ck_claim_amount"), CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_claim_currency"))
    id: Mapped[uuid.UUID] = identity()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_number: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    incident_date: Mapped[date | None] = mapped_column(Date)
    reported_at: Mapped[datetime] = timestamp()
    status: Mapped[ClaimStatus] = mapped_column(enum_type(ClaimStatus), nullable=False)
    claimed_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    organization: Mapped[Organization] = relationship(back_populates="claims", overlaps="policy,claims")
    policy: Mapped[Policy | None] = relationship(back_populates="claims", overlaps="organization,claims")
    documents: Mapped[list[ClaimDocument]] = relationship(back_populates="claim")
    analyses: Mapped[list[ClaimAnalysis]] = relationship(back_populates="claim")


class ClaimDocument(Base):
    __tablename__ = "claim_documents"
    id: Mapped[uuid.UUID] = identity()
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(80), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    processing_status: Mapped[ProcessingStatus] = mapped_column(enum_type(ProcessingStatus), nullable=False)
    created_at: Mapped[datetime] = timestamp()
    claim: Mapped[Claim] = relationship(back_populates="documents")


class PolicyDocument(Base):
    __tablename__ = "policy_documents"
    __table_args__ = (CheckConstraint("document_version > 0", name="ck_document_version"),)
    id: Mapped[uuid.UUID] = identity()
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = timestamp()
    policy: Mapped[Policy] = relationship(back_populates="documents")


class ClaimAnalysis(Base):
    __tablename__ = "claim_analyses"
    __table_args__ = (CheckConstraint("(status = 'completed' AND extracted_data IS NOT NULL AND error_code IS NULL) OR (status = 'failed' AND extracted_data IS NULL AND error_code IS NOT NULL)", name="ck_analysis_result"), CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_analysis_input_tokens"), CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_analysis_output_tokens"), CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_analysis_latency"))
    id: Mapped[uuid.UUID] = identity()
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"), nullable=False, index=True)
    status: Mapped[AnalysisStatus] = mapped_column(enum_type(AnalysisStatus), nullable=False)
    extracted_data: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    missing_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    uncertainties: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    had_policy_association: Mapped[bool | None]
    created_at: Mapped[datetime] = timestamp()
    claim: Mapped[Claim] = relationship(back_populates="analyses")


class DocumentProcessingRun(Base):
    __tablename__ = "document_processing_runs"
    __table_args__ = (
        CheckConstraint("(policy_document_id IS NOT NULL AND claim_document_id IS NULL AND policy_document_version IS NOT NULL) OR (policy_document_id IS NULL AND claim_document_id IS NOT NULL AND policy_document_version IS NULL)", name="ck_run_source"),
        CheckConstraint("status IN ('pending', 'processing', 'completed', 'partial', 'failed')", name="ck_run_status"),
        CheckConstraint("page_count > 0", name="ck_run_page_count"),
        UniqueConstraint("policy_document_id", "policy_document_version", "source_hash", "config_hash", name="uq_policy_extraction"),
        UniqueConstraint("claim_document_id", "source_hash", "config_hash", name="uq_claim_extraction"),
    )
    id: Mapped[uuid.UUID] = identity()
    policy_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policy_documents.id"))
    claim_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("claim_documents.id"))
    policy_document_version: Mapped[int | None] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(40), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages: Mapped[list[ExtractedPage]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="ExtractedPage.page_number")


class ExtractedPage(Base):
    __tablename__ = "extracted_pages"
    __table_args__ = (
        UniqueConstraint("run_id", "page_number", name="uq_run_page"),
        CheckConstraint("page_number > 0", name="ck_page_number"),
        CheckConstraint("status IN ('completed', 'blank', 'failed')", name="ck_page_status"),
        CheckConstraint("method IS NULL OR method IN ('native', 'ocr', 'mixed')", name="ck_page_method"),
    )
    id: Mapped[uuid.UUID] = identity()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_processing_runs.id", ondelete="CASCADE"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(String(80))
    width: Mapped[float] = mapped_column(nullable=False)
    height: Mapped[float] = mapped_column(nullable=False)
    rotation: Mapped[int] = mapped_column(Integer, nullable=False)
    run: Mapped[DocumentProcessingRun] = relationship(back_populates="pages")
    blocks: Mapped[list[ExtractedBlock]] = relationship(back_populates="page", cascade="all, delete-orphan", order_by="ExtractedBlock.block_index")


class ExtractedBlock(Base):
    __tablename__ = "extracted_blocks"
    __table_args__ = (
        UniqueConstraint("page_id", "block_index", name="uq_page_block"),
        CheckConstraint("block_index >= 0", name="ck_block_index"),
        CheckConstraint("method IN ('native', 'ocr')", name="ck_block_method"),
    )
    id: Mapped[uuid.UUID] = identity()
    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extracted_pages.id", ondelete="CASCADE"), nullable=False)
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    block_type: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    bbox: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    confidence: Mapped[float | None]
    page: Mapped[ExtractedPage] = relationship(back_populates="blocks")
