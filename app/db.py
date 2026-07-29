"""DB 엔진·세션 — SQLAlchemy 2.0.

기본은 settings.DATABASE_URL(Postgres). 테스트는 configure()로 SQLite로 교체.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base

_engine = None
_Session: sessionmaker | None = None


def _normalize(url: str) -> str:
    """클라우드 제공 URL(postgres://, postgresql://)을 psycopg3 드라이버로 정규화."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def configure(url: str | None = None) -> None:
    """엔진/세션 초기화. url 미지정 시 settings.DATABASE_URL 사용."""
    global _engine, _Session
    url = _normalize(url or settings.DATABASE_URL)
    kwargs = {"future": True}
    if url.startswith("sqlite"):
        kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _engine = create_engine(url, **kwargs)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_db() -> None:
    if _engine is None:
        configure()
    Base.metadata.create_all(_engine)
    _ensure_columns()


def _ensure_columns() -> None:
    """기존 테이블에 나중에 추가된 컬럼을 안전하게 보강(간이 마이그레이션).
    Postgres는 ADD COLUMN IF NOT EXISTS로 idempotent, SQLite는 예외 무시."""
    from sqlalchemy import text
    stmts = [
        "ALTER TABLE employees ADD COLUMN IF NOT EXISTS position VARCHAR DEFAULT '일반'",
        "ALTER TABLE employees ADD COLUMN IF NOT EXISTS display_name VARCHAR",
    ]
    for st in stmts:
        try:
            with _engine.begin() as conn:
                conn.execute(text(st))
        except Exception:
            pass


@contextmanager
def session_scope() -> Session:
    """트랜잭션 스코프. 커밋/롤백/클로즈 자동 처리."""
    if _Session is None:
        configure()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
