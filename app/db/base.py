from sqlalchemy import MetaData
from sqlalchemy.orm import declarative_base

# Enforce strict naming conventions for migrations and constraints
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}

# This is the single source of truth for the SQLAlchemy Base
Base = declarative_base(metadata=MetaData(naming_convention=convention))
