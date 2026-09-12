from invoices import days_until_due


def test_thirty_day_terms_ten_days_in():
    assert days_until_due("2026-03-01", 30, "2026-03-11") == 20


def test_due_today_is_zero():
    assert days_until_due("2026-03-01", 30, "2026-03-31") == 0


def test_overdue_is_negative():
    assert days_until_due("2026-03-01", 30, "2026-04-05") == -5


def test_crosses_month_end():
    assert days_until_due("2026-01-20", 15, "2026-01-31") == 4
