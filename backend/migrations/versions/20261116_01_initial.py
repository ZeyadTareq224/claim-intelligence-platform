"""Initial insurance domain schema.

Revision ID: 20261116_01
Revises:
"""
from alembic import op
from migrations.schema_v1 import Base

revision = "20261116_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
