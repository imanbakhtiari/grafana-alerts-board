# db.py
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, Column, Integer, String, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base


# --------- Build database URL from env with sensible fallbacks ---------
# Priority:
#   1) DATABASE_URL (full SQLAlchemy URL, e.g. sqlite:////app/data/reports.db)
#   2) DB_URL       (alias for convenience)
#   3) DB_PATH      (plain path like /app/data/reports.db) -> turned into sqlite:///...
#   4) fallback     sqlite:///reports.db (relative to the app working dir)
db_url = os.getenv("DATABASE_URL") or os.getenv("DB_URL")

if not db_url:
    db_path = os.getenv("DB_PATH")
    if db_path:
        # Ensure parent directory exists (only if a directory component is present)
        dirpath = os.path.dirname(db_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        db_url = f"sqlite:///{db_path}"

if not db_url:
    db_url = "sqlite:///reports.db"  # your current default (relative to /app)


# SQLite needs a special arg when used in threaded apps
connect_args = {}
if db_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

ENGINE = create_engine(db_url, echo=False, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False, future=True)
Base = declarative_base()


# --------- Helpers ---------
def utcnow():
    return datetime.now(timezone.utc)


# --------- Models ---------
class DcCount(Base):
    __tablename__ = "dc_counts"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), index=True, nullable=False)  # UTC snapshot time
    dc = Column(String, index=True, nullable=False)
    active = Column(Integer, nullable=False)      # non-suppressed
    suppressed = Column(Integer, nullable=False)  # suppressed total
    total = Column(Integer, nullable=False)       # active + suppressed


class AlertSnapshot(Base):
    __tablename__ = "alert_snapshots"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), index=True, nullable=False)  # snapshot time (UTC)
    dc = Column(String, index=True, nullable=False)

    alertname = Column(String, index=True)
    status = Column(String)                        # active|firing|suppressed
    fingerprint = Column(String, index=True)       # may be empty if Grafana didn’t send it
    source = Column(String)                        # grafana name

    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at   = Column(DateTime(timezone=True), nullable=True)

    labels = Column(JSON)        # optional
    annotations = Column(JSON)   # optional


def init_db():
    # Create tables if they don't exist
    Base.metadata.create_all(ENGINE)

