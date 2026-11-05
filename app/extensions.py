"""Flask extensions, created here and bound to the app in ``create_app``."""

from __future__ import annotations

import json
from typing import Any

from cachelib.serializers import BaseSerializer
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Every constraint has a predictable name, so later migrations can refer to it.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class JsonSerializer(BaseSerializer):
    """How values are stored in Redis.

    The default is pickle, which runs code when it reads. With JSON, someone who gets into Redis
    can spoil a cached figure for a few minutes, but can't take over the app.
    """

    def dumps(self, value: Any, protocol: int | None = None) -> bytes:
        return json.dumps(value).encode()

    def loads(self, value: bytes | None) -> Any:
        return None if value is None else json.loads(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


db = SQLAlchemy(model_class=Base, disable_autonaming=True)
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
cache = Cache(with_jinja2_ext=False)
