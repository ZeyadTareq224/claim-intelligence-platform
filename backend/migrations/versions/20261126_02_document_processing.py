"""Persist document extraction provenance.

Revision ID: 20261126_02
Revises: 20261116_01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261126_02"
down_revision = "20261116_01"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table("document_processing_runs",
        sa.Column("id", uid, primary_key=True),
        sa.Column("policy_document_id", uid, sa.ForeignKey("policy_documents.id")),
        sa.Column("claim_document_id", uid, sa.ForeignKey("claim_documents.id")),
        sa.Column("policy_document_version", sa.Integer),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("extractor_version", sa.String(40), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("page_count", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("(policy_document_id IS NOT NULL AND claim_document_id IS NULL AND policy_document_version IS NOT NULL) OR (policy_document_id IS NULL AND claim_document_id IS NOT NULL AND policy_document_version IS NULL)", name="ck_run_source"),
        sa.CheckConstraint("status IN ('completed', 'partial', 'failed')", name="ck_run_status"),
        sa.CheckConstraint("page_count > 0", name="ck_run_page_count"),
        sa.UniqueConstraint("policy_document_id", "policy_document_version", "source_hash", "config_hash", name="uq_policy_extraction"),
        sa.UniqueConstraint("claim_document_id", "source_hash", "config_hash", name="uq_claim_extraction"))
    op.create_table("extracted_pages",
        sa.Column("id", uid, primary_key=True),
        sa.Column("run_id", uid, sa.ForeignKey("document_processing_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("method", sa.String(20)), sa.Column("reason", sa.String(80)), sa.Column("error", sa.String(80)),
        sa.Column("width", sa.Float, nullable=False), sa.Column("height", sa.Float, nullable=False),
        sa.Column("rotation", sa.Integer, nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_page_number"),
        sa.CheckConstraint("status IN ('completed', 'blank', 'failed')", name="ck_page_status"),
        sa.CheckConstraint("method IS NULL OR method IN ('native', 'ocr', 'mixed')", name="ck_page_method"),
        sa.UniqueConstraint("run_id", "page_number", name="uq_run_page"))
    op.create_table("extracted_blocks",
        sa.Column("id", uid, primary_key=True),
        sa.Column("page_id", uid, sa.ForeignKey("extracted_pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("block_index", sa.Integer, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=False), sa.Column("normalized_text", sa.Text, nullable=False),
        sa.Column("block_type", sa.String(20), nullable=False), sa.Column("method", sa.String(20), nullable=False),
        sa.Column("bbox", postgresql.JSONB), sa.Column("confidence", sa.Float),
        sa.CheckConstraint("block_index >= 0", name="ck_block_index"),
        sa.CheckConstraint("method IN ('native', 'ocr')", name="ck_block_method"),
        sa.UniqueConstraint("page_id", "block_index", name="uq_page_block"))


def downgrade():
    op.drop_table("extracted_blocks")
    op.drop_table("extracted_pages")
    op.drop_table("document_processing_runs")
