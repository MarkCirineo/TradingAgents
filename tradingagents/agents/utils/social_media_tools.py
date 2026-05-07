from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor

@tool
def get_social_sentiment(
    ticker: Annotated[str, "Ticker symbol"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "Number of days to look back"] = 7,
) -> str:
    """
    Retrieve social media sentiment data for a given ticker symbol.
    Fetches aggregated sentiment from Reddit and Twitter including
    mention counts, positive/negative ratios, and sentiment scores.
    Uses the configured social_sentiment vendor (default: Finnhub).
    Args:
        ticker (str): Ticker symbol (e.g. AAPL, NVDA)
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back (default 7)
    Returns:
        str: A formatted report of social media sentiment data
    """
    return route_to_vendor("get_social_sentiment", ticker, curr_date, look_back_days)
