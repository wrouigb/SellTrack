"""Business logic. Routes stay thin and call into these modules.

Conventions:

* Every function that touches a user's records takes ``user_id`` and filters by it. A record in
  another account is reported as :class:`~app.services.errors.NotFoundError`, exactly as if it
  didn't exist.
* A public function that changes data is one transaction: it commits on success. Helpers that
  only add to the current transaction (``audit.record``, ``notifications.notify``) don't commit.
* Input is validated here, not just in forms, and problems raise
  :class:`~app.services.errors.ValidationFailed` with a message per field.
"""
