"""Safe, user-facing write refusal and uncertain-effect errors."""


class VaultError(Exception):
    pass


class InvalidJournal(VaultError, ValueError):
    def __init__(self):
        super().__init__("Receipt journal is invalid; no changes were made.")


class VaultBusy(VaultError):
    def __init__(self):
        super().__init__("vault is busy — try again")


class VaultReadOnly(VaultError):
    def __init__(self):
        super().__init__("read-only: another Vault window holds this vault")


class VaultWriteBlocked(VaultError):
    def __init__(self):
        super().__init__("an operation may have applied; writes blocked until restart")


class MayHaveApplied(VaultError, OSError):
    def __init__(self):
        super().__init__("operation may have applied; writes blocked until restart")
