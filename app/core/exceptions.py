class LedgerException(Exception):
    """Base exception for all ledger domain errors."""
    pass


class InsufficientFundsError(LedgerException):
    """Raised when a debit would violate the account's overdraft limit."""
    def __init__(self, account_id: str, available: float, requested: float):
        self.account_id = account_id
        self.available = available
        self.requested = requested
        super().__init__(
            f"Account {account_id} has insufficient funds. "
            f"Available: {available:.2f}, Requested: {requested:.2f}"
        )


class AccountNotFoundError(LedgerException):
    def __init__(self, account_id: str):
        super().__init__(f"Account '{account_id}' not found.")


class AccountAlreadyExistsError(LedgerException):
    def __init__(self, account_id: str):
        super().__init__(f"Account '{account_id}' already exists.")


class DoubleEntryViolationError(LedgerException):
    """Raised when debit and credit legs of a transaction don't net to zero."""
    def __init__(self, net: float):
        super().__init__(
            f"Double-entry invariant violated: net of all legs is {net:.6f}, expected 0.0"
        )


class TransactionAmountError(LedgerException):
    def __init__(self, amount: float, max_amount: float):
        super().__init__(
            f"Transaction amount {amount:.2f} exceeds maximum allowed {max_amount:.2f}"
        )


class NegativeAmountError(LedgerException):
    def __init__(self, amount: float):
        super().__init__(f"Transaction amount must be positive, got {amount:.2f}")


class CurrencyMismatchError(LedgerException):
    def __init__(self, expected: str, got: str):
        super().__init__(
            f"Currency mismatch: account currency is {expected}, transaction uses {got}"
        )


class AccountClosedError(LedgerException):
    def __init__(self, account_id: str):
        super().__init__(f"Account '{account_id}' is closed and cannot accept transactions.")


class SnapshotNotFoundError(LedgerException):
    def __init__(self, account_id: str):
        super().__init__(f"No snapshot found for account '{account_id}'.")
