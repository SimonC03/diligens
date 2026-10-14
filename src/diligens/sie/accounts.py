"""BAS account classes, see docs/DOMAIN.md sections 1 and 2."""


def is_balance_account(account: str) -> bool:
    """Class 1 and 2: assets, equity and liabilities (balansräkning)."""
    return account[:1] in {"1", "2"}


def is_result_account(account: str) -> bool:
    """Class 3 to 8: revenue, costs and financial items (resultaträkning)."""
    return account[:1] in {"3", "4", "5", "6", "7", "8"}
