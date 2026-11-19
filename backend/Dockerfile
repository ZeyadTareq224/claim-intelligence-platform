FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/claim-platform

COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/fixtures ./fixtures
COPY frontend/requirements.txt /tmp/frontend-requirements.txt
RUN pip install --no-cache-dir . -r /tmp/frontend-requirements.txt
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations

COPY frontend ./frontend
COPY examples ./examples

# Database migrations are run explicitly before launching the UI.
CMD ["streamlit", "run", "frontend/main.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
