import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def make_sessions():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is required")
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)
