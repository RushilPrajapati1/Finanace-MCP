"""Domain layer: the framework-agnostic accounting model.

Nothing in this package imports SQLAlchemy, FastAPI, or the database. It holds
the rules that make a ledger a *correct* double-entry ledger so they can be
reasoned about and unit-tested in isolation.
"""
