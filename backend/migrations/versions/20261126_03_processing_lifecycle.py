"""Track document processing lifecycle.

Revision ID: 20261126_03
Revises: 20261126_02
"""
from alembic import op
import sqlalchemy as sa

revision = "20261126_03"
down_revision = "20261126_02"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_run_status", "document_processing_runs", type_="check")
    op.create_check_constraint("ck_run_status", "document_processing_runs", "status IN ('pending', 'processing', 'completed', 'partial', 'failed')")
    op.add_column("document_processing_runs", sa.Column("error_code", sa.String(80)))
    op.add_column("document_processing_runs", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("document_processing_runs", sa.Column("finished_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("document_processing_runs", "finished_at")
    op.drop_column("document_processing_runs", "started_at")
    op.drop_column("document_processing_runs", "error_code")
    op.drop_constraint("ck_run_status", "document_processing_runs", type_="check")
    op.create_check_constraint("ck_run_status", "document_processing_runs", "status IN ('completed', 'partial', 'failed')")
