from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL, database_backend


engine_options = {
    "pool_pre_ping": True,
}
if database_backend() == "sqlite":
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_options)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_database():
    database = SessionLocal()

    try:
        yield database
    finally:
        database.close()
