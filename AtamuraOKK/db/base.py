from sqlalchemy.orm import DeclarativeBase

from AtamuraOKK.db.meta import meta


class Base(DeclarativeBase):
    """Base for all models."""

    metadata = meta
