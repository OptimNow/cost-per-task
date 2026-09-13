from datetime import date, timedelta


def days_until_due(issued: str, payment_terms_days: int, today: str) -> int:
    """Days remaining until an invoice is due (negative when overdue).

    Dates are ISO strings, YYYY-MM-DD.
    """
    issued_on = date.fromisoformat(issued)
    due_on = issued_on + timedelta(days=payment_terms_days)
    current = date.fromisoformat(today)
    return (current - due_on).days
