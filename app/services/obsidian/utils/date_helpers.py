from datetime import datetime, timedelta

DAY_ROLLOVER_HOUR = 3


def get_effective_date(now: datetime) -> datetime:
    """Get the effective date, treating midnight-3am as the previous day."""
    if now.hour < DAY_ROLLOVER_HOUR:
        return now - timedelta(days=1)
    return now
