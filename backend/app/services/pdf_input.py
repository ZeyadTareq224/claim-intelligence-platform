"""Validate and preserve PDF inputs before document processing."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from uuid import UUID

import pymupdf

from app.domain.models import ClaimDocument, PolicyDocument


class PdfInputError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedPdf:
    source_type: str
    document_id: UUID
    sha256: str
    page_count: int


class PdfInputService:
    def __init__(self, sessions, storage_dir: Path, max_bytes: int = 25 * 1024 * 1024, max_pages: int = 200):
        if max_bytes <= 0 or max_pages <= 0:
            raise ValueError("PDF limits must be positive")
        self.sessions = sessions
        self.storage_dir = storage_dir
        self.max_bytes = max_bytes
        self.max_pages = max_pages

    @classmethod
    def from_environment(cls, sessions):
        return cls(
            sessions,
            Path(os.environ.get("PDF_STORAGE_DIR", ".document-storage")),
            int(os.environ.get("PDF_MAX_BYTES", str(25 * 1024 * 1024))),
            int(os.environ.get("PDF_MAX_PAGES", "200")),
        )

    def validate(self, source_type: str, document_id: UUID, path: Path) -> ValidatedPdf:
        models = {"policy": PolicyDocument, "claim": ClaimDocument}
        if source_type not in models:
            raise PdfInputError("invalid_source_type")
        with self.sessions() as session:
            if session.get(models[source_type], document_id) is None:
                raise PdfInputError("document_not_found")

        try:
            # Nonblocking open prevents a supplied FIFO from hanging the command.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise PdfInputError("file_not_regular")
            with os.fdopen(fd, "rb") as source:
                data = source.read(self.max_bytes + 1)
        except OSError as exc:
            raise PdfInputError("file_unreadable") from exc
        if len(data) > self.max_bytes:
            raise PdfInputError("file_too_large")
        if not data.startswith(b"%PDF-"):
            raise PdfInputError("invalid_pdf")
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                if pdf.needs_pass or pdf.is_encrypted or pdf.metadata.get("encryption"):
                    raise PdfInputError("encrypted_pdf")
                if pdf.is_repaired or pdf.page_count == 0:
                    raise PdfInputError("invalid_pdf")
                if pdf.page_count > self.max_pages:
                    raise PdfInputError("page_limit_exceeded")
                count = pdf.page_count
                for page in pdf:
                    _ = page.rect
        except PdfInputError:
            raise
        except (RuntimeError, ValueError) as exc:
            raise PdfInputError("invalid_pdf") from exc

        digest = hashlib.sha256(data).hexdigest()
        self._preserve(data, digest)
        return ValidatedPdf(source_type, document_id, digest, count)

    def _preserve(self, data: bytes, digest: str) -> None:
        try:
            self.storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.storage_dir / f"{digest}.pdf"
            with tempfile.NamedTemporaryFile(dir=self.storage_dir, prefix=".pdf-", delete=False) as tmp:
                name = Path(tmp.name)
                try:
                    tmp.write(data)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    try:
                        os.link(name, target)
                    except FileExistsError:
                        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                            raise PdfInputError("storage_conflict")
                finally:
                    name.unlink(missing_ok=True)
        except OSError as exc:
            raise PdfInputError("storage_error") from exc
