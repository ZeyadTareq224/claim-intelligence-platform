"""Add partial claim-document status and backfill from existing runs.

Revision ID: 20261127_04
Revises: 20261126_03
"""
from alembic import op

revision = "20261127_04"
down_revision = "20261126_03"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("processingstatus", "claim_documents", type_="check")
    op.create_check_constraint("processingstatus", "claim_documents",
        "processing_status IN ('pending', 'processing', 'completed', 'partial', 'failed')")
    op.execute("""
        UPDATE claim_documents AS document
        SET processing_status = latest.status
        FROM (
            SELECT DISTINCT ON (claim_document_id) claim_document_id, status
            FROM document_processing_runs
            WHERE claim_document_id IS NOT NULL
            ORDER BY claim_document_id, COALESCE(started_at, created_at) DESC, created_at DESC, id DESC
        ) AS latest
        WHERE document.id = latest.claim_document_id
    """)


def downgrade():
    op.drop_constraint("processingstatus", "claim_documents", type_="check")
    # Downgrade fails rather than silently rewriting existing partial statuses.
    op.create_check_constraint("processingstatus", "claim_documents",
        "processing_status IN ('pending', 'processing', 'completed', 'failed')")
