from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool


@tool
def get_current_time(timezone: str = "Asia/Seoul") -> str:
    """Return the current date and time in the given IANA timezone (e.g. 'Asia/Seoul', 'UTC')."""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except ZoneInfoNotFoundError:
        return f"Unknown timezone: {timezone}"
    return now.isoformat(timespec="seconds")


BASIC_TOOLS = [get_current_time]
