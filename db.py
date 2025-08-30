# db.py
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base

ENGINE = create_engine("sqlite:///reports.db", echo=False, future=True)
SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False, future=True)
Base = declarative_base()

def utcnow():
    return datetime.now(timezone.utc)

class DcCount(Base):
    __tablename__ = "dc_counts"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), index=True, nullable=False)  # UTC
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
    status = Column(String)              # active|firing|suppressed
    fingerprint = Column(String, index=True)  # may be empty if Grafana didn’t send it
    source = Column(String)              # grafana name

    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at   = Column(DateTime(timezone=True), nullable=True)

    labels = Column(JSON)        # optional, useful for forensics
    annotations = Column(JSON)   # optional

def init_db():
    Base.metadata.create_all(ENGINE)

