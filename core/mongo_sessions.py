"""
Django session backend that keeps login sessions in MongoDB (Atlas).

With this, nothing important lives on the web server's disk: users, medicines,
reports AND login sessions are all in Atlas, so hosts with a temporary
filesystem (e.g. Render free) can restart or redeploy without logging
everybody out.

Enabled in settings.py with  SESSION_ENGINE = "core.mongo_sessions".
Expired sessions are removed by a MongoDB TTL index on ``expire_at``.
"""

from datetime import datetime, timedelta, timezone

from django.contrib.sessions.backends.base import CreateError, SessionBase, UpdateError
from pymongo.errors import DuplicateKeyError


def _collection():
    from .db import db

    if not db.online:
        raise RuntimeError("MongoDB is offline: cannot use sessions.")

    return db.db.sessions


def _now():
    return datetime.now(timezone.utc)


class SessionStore(SessionBase):
    def __init__(self, session_key=None):
        super().__init__(session_key)

    def load(self):
        try:
            doc = _collection().find_one(
                {"_id": self.session_key, "expire_at": {"$gt": _now()}}
            )
        except Exception:
            doc = None

        if doc:
            try:
                return self.decode(doc["data"])
            except Exception:
                pass

        self._session_key = None
        return {}

    def exists(self, session_key):
        if not session_key:
            return False

        return (
            _collection().count_documents(
                {"_id": session_key, "expire_at": {"$gt": _now()}}, limit=1
            )
            > 0
        )

    def create(self):
        for _ in range(10000):
            self._session_key = self._get_new_session_key()

            try:
                self.save(must_create=True)
            except CreateError:
                continue

            self.modified = True
            return

        raise RuntimeError("Unable to create a unique session key.")

    def save(self, must_create=False):
        if self.session_key is None:
            return self.create()

        data = self.encode(self._get_session(no_load=must_create))
        expire_at = _now() + timedelta(seconds=self.get_expiry_age())
        collection = _collection()

        if must_create:
            try:
                collection.insert_one(
                    {"_id": self.session_key, "data": data, "expire_at": expire_at}
                )
            except DuplicateKeyError:
                raise CreateError
        else:
            result = collection.update_one(
                {"_id": self.session_key},
                {"$set": {"data": data, "expire_at": expire_at}},
            )

            if result.matched_count == 0:
                raise UpdateError

    def delete(self, session_key=None):
        if session_key is None:
            if self.session_key is None:
                return

            session_key = self.session_key

        _collection().delete_one({"_id": session_key})

    @classmethod
    def clear_expired(cls):
        _collection().delete_many({"expire_at": {"$lte": _now()}})
