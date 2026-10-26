from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa
from app.config import get_settings
from app.db import Base

target_metadata = Base.metadata


def run():
    eng = create_engine(get_settings().database_url)
    with eng.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run()
