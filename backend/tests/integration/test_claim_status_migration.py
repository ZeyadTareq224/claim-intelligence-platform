"""Exercise the real status migration on existing records in an isolated schema."""
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.domain.models import (Claim, ClaimDocument, ClaimStatus, DocumentProcessingRun,
                               Organization, ProcessingStatus)


def test_migration_backfills_latest_run_without_changing_unrelated_documents(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")
    schema = f"status_migration_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    scoped = create_engine(scoped_url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    monkeypatch.setenv("DATABASE_URL", scoped_url.render_as_string(hide_password=False))
    org_id, claim_id, first_id, second_id, untouched_id = (uuid4() for _ in range(5))
    try:
        command.upgrade(config, "20261126_03")
        sessions = sessionmaker(scoped, expire_on_commit=False)
        earlier = datetime(2024, 1, 1, tzinfo=timezone.utc)
        later = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with sessions.begin() as session:
            session.add(Organization(id=org_id, name="Migration test", code=org_id.hex))
            session.flush()
            session.add(Claim(id=claim_id, organization_id=org_id, claim_number="C1", description="Synthetic", status=ClaimStatus.DRAFT))
            session.flush()
            for document_id, status in ((first_id, ProcessingStatus.PENDING), (second_id, ProcessingStatus.PENDING),
                                        (untouched_id, ProcessingStatus.COMPLETED)):
                session.add(ClaimDocument(id=document_id, claim_id=claim_id, document_type="report", filename="test.pdf",
                    content_type="application/pdf", storage_key="synthetic", processing_status=status))
            session.flush()
            for document_id, status, timestamp, key in ((first_id, "completed", earlier, "a"),
                                                        (first_id, "partial", later, "b"),
                                                        (second_id, "failed", later, "c")):
                session.add(DocumentProcessingRun(claim_document_id=document_id, source_hash=key * 64,
                    config_hash=key * 64, extractor_version="test", config={}, status=status,
                    page_count=1, created_at=timestamp, started_at=timestamp))
        command.upgrade(config, "head")
        with sessions() as session:
            assert session.get(ClaimDocument, first_id).processing_status == ProcessingStatus.PARTIAL
            assert session.get(ClaimDocument, second_id).processing_status == ProcessingStatus.FAILED
            assert session.get(ClaimDocument, untouched_id).processing_status == ProcessingStatus.COMPLETED
        with pytest.raises(IntegrityError):
            command.downgrade(config, "20261126_03")
        with scoped.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261127_04"
            assert connection.scalar(text("SELECT processing_status FROM claim_documents WHERE id = :id"), {"id": first_id}) == "partial"
    finally:
        scoped.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
