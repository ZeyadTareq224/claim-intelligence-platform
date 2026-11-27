"""Local administration UI for the insurance domain."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

import streamlit as st
from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, Numeric, String, Text, select, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from app.persistence.db import make_sessions
from app.services.pdf_input import PdfInputError
from app.services.document_ingestion import DocumentIngestionService
from app.repositories.documents import DocumentRepository
from app.domain.models import (
    Organization, Vehicle, Policy, PolicyClause, Claim, ClaimDocument,
    PolicyDocument, ClaimAnalysis,
)

MODELS = {
    "Organizations": Organization,
    "Vehicles": Vehicle,
    "Policies": Policy,
    "Policy clauses": PolicyClause,
    "Claims": Claim,
    "Claim documents": ClaimDocument,
    "Policy documents": PolicyDocument,
    "Claim analyses": ClaimAnalysis,
}
READ_ONLY = {"id", "created_at", "uploaded_at", "reported_at"}
LABEL_FIELDS = ("name", "code", "claim_number", "policy_number", "plate_number", "clause_code", "filename", "model_name")


@st.cache_resource
def get_sessions():
    return make_sessions()


def record_label(record):
    parts = [str(getattr(record, field)) for field in LABEL_FIELDS if hasattr(record, field) and getattr(record, field)]
    return f"{' · '.join(parts)} — {record.id}" if parts else str(record.id)


def parse_value(column, value):
    """Convert editable text to the matching database type."""
    if value is None:
        return None
    typ = column.type
    if isinstance(typ, JSONB):
        parsed = json.loads(value)
        if column.name in {"missing_fields", "uncertainties"} and not isinstance(parsed, list):
            raise ValueError(f"{column.name} must be a JSON array")
        if column.name == "extracted_data" and not isinstance(parsed, dict):
            raise ValueError("extracted_data must be a JSON object")
        return parsed
    if isinstance(typ, Numeric):
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError(f"{column.name} must be a finite amount")
        return number
    if isinstance(typ, DateTime):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{column.name} must include a time zone offset")
        return parsed
    if isinstance(typ, Date):
        return date.fromisoformat(value)
    if typ.python_type is UUID:
        return UUID(value)
    return value


def error_message(error):
    if isinstance(error, IntegrityError):
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        return f"The database rejected this change{f' ({constraint})' if constraint else ''}. Check required fields, relationships and uniqueness."
    if isinstance(error, SQLAlchemyError):
        return "The database operation failed. Check the connection and related records."
    return str(error)


def field_input(column, value, session, key):
    typ = column.type
    label = column.name.replace("_", " ").title()
    foreign = next((fk for fk in column.foreign_keys if fk.column.primary_key), None)
    if foreign:
        target = foreign.column.table
        model = next((m for m in MODELS.values() if m.__table__ is target), None)
        if model:
            records = session.scalars(select(model).order_by(model.id)).all()
            choices = {record.id: record_label(record) for record in records}
            if value is not None and value not in choices:
                choices[value] = str(value)
            options = [None] + list(choices)
            return st.selectbox(label, options, index=options.index(value) if value in options else 0,
                                format_func=lambda x: choices.get(x, "Select a record"), key=key)
    if isinstance(typ, Enum):
        options = list(typ.enum_class)
        if column.nullable:
            options.insert(0, None)
        return st.selectbox(label, options, index=options.index(value) if value in options else 0,
                            format_func=lambda x: x.value.replace("_", " ").title() if x is not None else "None", key=key)
    if isinstance(typ, Boolean):
        options = [None, True, False] if column.nullable else [True, False]
        return st.selectbox(label, options, index=options.index(value) if value in options else 0, key=key)
    if isinstance(typ, JSONB):
        default = value if value is not None else ([] if column.name in {"missing_fields", "uncertainties"} else {})
        return st.text_area(label + " (JSON)", value=json.dumps(default, ensure_ascii=False, indent=2), key=key)
    if isinstance(typ, Integer):
        return st.number_input(label, value=int(value) if value is not None else 0, step=1, key=key)
    text = "" if value is None else str(value)
    if isinstance(typ, Text):
        return st.text_area(label, value=text, key=key)
    if isinstance(typ, DateTime):
        label += " (ISO 8601 with time zone)"
    elif isinstance(typ, Date):
        label += " (YYYY-MM-DD)"
    elif isinstance(typ, Numeric):
        label += " (decimal)"
    return st.text_input(label, value=text, key=key)


def edit_form(model, instance, session, form_key):
    data = {}
    with st.form(form_key):
        for column in model.__table__.columns:
            if column.name in READ_ONLY:
                continue
            value = getattr(instance, column.name, None) if instance is not None else None
            st.markdown(f"**{column.name.replace('_', ' ').title()}**" + (" · optional" if column.nullable else " · required"))
            raw = field_input(column, value, session, f"{form_key}_{column.name}")
            if column.nullable and not column.foreign_keys and not isinstance(column.type, (Enum, Boolean)):
                empty = st.checkbox("Leave empty", value=value is None, key=f"{form_key}_{column.name}_null")
                if empty:
                    raw = None
            data[column.name] = raw
        submitted = st.form_submit_button("Save changes" if instance is not None else "Create record", type="primary")
    if not submitted:
        return
    try:
        values = {col.name: parse_value(col, data[col.name]) for col in model.__table__.columns if col.name in data}
        if instance is None:
            session.add(model(**values))
        else:
            for name, value in values.items():
                setattr(instance, name, value)
        session.commit()
        st.session_state.notice = "Record saved."
        st.rerun()
    except (ValueError, InvalidOperation, SQLAlchemyError, TypeError) as exc:
        session.rollback()
        st.error(error_message(exc))



def upload_pdf(sessions, session):
    st.header("PDF upload")
    st.caption("Process a PDF for an existing policy or claim document.")
    source_type = st.selectbox("Document type", ("policy", "claim"), format_func=str.title)
    model = PolicyDocument if source_type == "policy" else ClaimDocument
    records = session.scalars(select(model).order_by(model.id)).all()
    if not records:
        st.info("Create a document record first using the sidebar.")
        return
    choices = {record.id: record_label(record) for record in records}
    document_id = st.selectbox("Existing document", list(choices), format_func=choices.get)
    uploaded = st.file_uploader("Choose a PDF", type=["pdf"])
    repository = DocumentRepository(sessions)
    if st.button("Process PDF", type="primary", disabled=uploaded is None):
        try:
            service = DocumentIngestionService.from_environment(sessions)
            if uploaded.size > service.inputs.max_bytes:
                raise PdfInputError("file_too_large")
            with tempfile.TemporaryDirectory(prefix="claim-pdf-") as directory:
                path = Path(directory) / "upload.pdf"
                path.write_bytes(uploaded.getvalue())
                run_id = service.ingest(source_type, document_id, path)
            result = repository.get_run(run_id)
            if result["status"] == "completed":
                st.success(f"PDF processed. Run: {run_id}")
            elif result["status"] == "partial":
                st.warning(f"PDF partially processed. Run: {run_id}. Check the page errors below.")
            else:
                st.error(f"PDF processing failed. Run: {run_id}. Check the errors below.")
        except PdfInputError as exc:
            st.error(f"PDF input error: {exc.code}")
        except (OSError, RuntimeError, ValueError, SQLAlchemyError):
            st.error("PDF processing failed. Check the run history below and the database connection.")

    include_text = st.checkbox("Show full extracted text")
    runs = repository.inspect(source_type, document_id, include_text=include_text)
    if not runs:
        st.info("No processing runs for this document yet.")
    for run in runs:
        with st.expander(f"Run {run['run_id']} · {run['status']}"):
            st.write(f"Source SHA-256: {run['source_hash']}")
            st.write(f"Pages: {len(run['pages'])} / {run['page_count']} · Extractor: {run['extractor_version']}")
            if run["error_code"]:
                st.error(f"Run error: {run['error_code']}")
            for page in run["pages"]:
                st.write(f"Page {page['page_number']} · {page['status']} · {page['method'] or 'none'}")
                if page["reason"]:
                    st.caption(f"Extraction reason: {page['reason']}")
                if page["error"]:
                    st.error(f"Page error: {page['error']}")
                if include_text and page["blocks"]:
                    st.text("\n".join(block["raw_text"] for block in page["blocks"]))
                elif page["preview"]:
                    st.text(page["preview"])
                if page["blocks"]:
                    st.caption(f"Page size: {page['width']} × {page['height']} points · Rotation: {page['rotation']}°")
                    st.dataframe(page["blocks"], hide_index=True, width="stretch")


def main():
    st.set_page_config(page_title="Claim Intelligence", page_icon="◈", layout="wide")
    st.title("Claim Intelligence")
    st.caption("Local insurance data workspace")
    st.sidebar.title("Data")
    choice = st.sidebar.radio("Entity", list(MODELS) + ["PDF upload"])
    if choice == "PDF upload":
        try:
            sessions = get_sessions()
            with sessions() as session:
                upload_pdf(sessions, session)
        except (ValueError, SQLAlchemyError) as exc:
            st.error(error_message(exc))
        return
    model = MODELS[choice]
    st.header(choice)
    if notice := st.session_state.pop("notice", None):
        st.success(notice)
    try:
        sessions = get_sessions()
        with sessions() as session:
            total = session.scalar(select(func.count()).select_from(model))
            st.caption(f"{total} records")
            records = session.scalars(select(model).order_by(model.id)).all()
            if records:
                rows = [{column.name: str(getattr(row, column.name)) if getattr(row, column.name) is not None else None
                         for column in model.__table__.columns} for row in records]
                st.dataframe(rows, hide_index=True, width="stretch")
            else:
                st.info("No records yet.")
            create, manage = st.tabs(["Create", "Edit or delete"])
            with create:
                edit_form(model, None, session, f"create_{choice}")
            with manage:
                if not records:
                    st.info("Create a record first.")
                else:
                    ids = {row.id: row for row in records}
                    selected = st.selectbox("Select a record", list(ids), format_func=lambda ident: record_label(ids[ident]), key=f"select_{choice}")
                    instance = ids[selected]
                    edit_form(model, instance, session, f"edit_{choice}_{selected}")
                    with st.expander("Delete record"):
                        st.warning("Deletion is permanent. Related records may prevent deletion.")
                        confirm = st.checkbox("I understand this permanently deletes the selected record", key=f"confirm_{choice}_{selected}")
                        if st.button("Delete", disabled=not confirm, key=f"delete_{choice}_{selected}"):
                            try:
                                session.delete(instance)
                                session.commit()
                                st.session_state.notice = "Record deleted."
                                st.rerun()
                            except SQLAlchemyError as exc:
                                session.rollback()
                                st.error(error_message(exc))
    except (ValueError, SQLAlchemyError) as exc:
        st.error(error_message(exc))


if __name__ == "__main__":
    main()
