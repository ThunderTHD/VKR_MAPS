"""ORM-модели. Каждая модель импортируется здесь, чтобы Alembic видел все таблицы."""
from app.core.db import Base

__all__ = ["Base"]
