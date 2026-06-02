"""Service layer: the transactional use-cases of the ledger.

Services own their database transaction boundaries (``commit``/``rollback``) and
enforce the accounting invariants. The API layer is a thin adapter over these
functions; you could equally drive them from a queue consumer, a CLI, or tests.
"""
