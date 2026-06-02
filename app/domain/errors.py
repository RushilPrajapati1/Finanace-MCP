"""Domain errors.

Each error carries a stable machine-readable ``code`` and the HTTP
``status_code`` the API should map it to. The API layer registers a single
handler for ``LedgerError`` (see ``app/api/errors.py``), so raising one of these
from anywhere in the stack produces a consistent JSON error response.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for all expected domain failures."""

    status_code: int = 400
    code: str = "ledger_error"

    def __init__(self, message: str | None = None):
        self.message = message or (self.__doc__ or self.code)
        super().__init__(self.message)


class ValidationError(LedgerError):
    """The request is malformed or violates a ledger rule."""

    code = "validation_error"


class UnbalancedTransactionError(LedgerError):
    """Debits and credits do not balance for every currency in the transaction."""

    code = "unbalanced_transaction"
    status_code = 422


class CurrencyMismatchError(LedgerError):
    """A posting's currency does not match its account's currency."""

    code = "currency_mismatch"
    status_code = 422


class CurrencyNotFoundError(LedgerError):
    """The requested currency is not registered in the ledger."""

    code = "currency_not_found"
    status_code = 404


class AccountNotFoundError(LedgerError):
    """No such account for this tenant."""

    code = "account_not_found"
    status_code = 404


class InactiveAccountError(LedgerError):
    """The account has been deactivated and cannot receive postings."""

    code = "inactive_account"
    status_code = 409


class DuplicateAccountError(LedgerError):
    """An account with this external_id already exists for the tenant."""

    code = "duplicate_account"
    status_code = 409


class TransactionNotFoundError(LedgerError):
    """No such transaction for this tenant."""

    code = "transaction_not_found"
    status_code = 404


class AlreadyReversedError(LedgerError):
    """The transaction has already been reversed."""

    code = "already_reversed"
    status_code = 409


class AuthenticationError(LedgerError):
    """The API key is missing, revoked, or invalid."""

    code = "authentication_error"
    status_code = 401
