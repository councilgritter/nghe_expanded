"""Storage for the reading module: connections, migrations and the dictionary.

Kept deliberately thin.  ``functions`` are free functions over a
:class:`sqlite3.Connection` rather than an ORM, matching the repo's preference for
plain stdlib Python over frameworks.
"""
