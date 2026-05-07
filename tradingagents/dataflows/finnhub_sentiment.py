"""Finnhub Sentiment data provider.

Fetches insider sentiment and analyst recommendation trends for a given
ticker symbol using the Finnhub API free tier.

Free tier: 60 API calls/minute.  The social media analyst typically makes
1-2 calls per pipeline run, well within limits.

Endpoints used (all free tier):
- /stock/insider-sentiment -- Monthly insider buy/sell ratios (MSPR)
- /stock/recommendation -- Analyst consensus trends (strong buy to strong sell)

API docs: https://finnhub.io/docs/api
"""

import os
import requests
from datetime import datetime, timedelta


_BASE_URL = "https://finnhub.io/api/v1"


def _get_api_key() -> str:
    """Retrieve the Finnhub API key from the environment."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        raise RuntimeError(
            "FINNHUB_API_KEY not set.  Get a free key at https://finnhub.io "
            "and add it to your .env file."
        )
    return key


def _fetch_insider_sentiment(ticker: str, api_key: str, start_date: str, end_date: str) -> str:
    """Fetch insider sentiment (MSPR -- Monthly Share Purchase Ratio)."""
    params = {
        "symbol": ticker,
        "from": start_date,
        "to": end_date,
        "token": api_key,
    }

    try:
        resp = requests.get(f"{_BASE_URL}/stock/insider-sentiment", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return f"Error fetching insider sentiment: {e}"

    entries = data.get("data", [])
    if not entries:
        return "No insider sentiment data available for this period."

    lines = [
        "### Insider Sentiment (Monthly Share Purchase Ratio)",
        "",
        "MSPR > 0 indicates net insider buying (bullish signal).",
        "MSPR < 0 indicates net insider selling (bearish signal).",
        "",
        "| Month | Year | MSPR | Net Shares Changed |",
        "|-------|------|------|--------------------|",
    ]

    for entry in entries[-6:]:  # Last 6 months max
        lines.append(
            f"| {entry.get('month', 'N/A')} "
            f"| {entry.get('year', 'N/A')} "
            f"| {entry.get('mspr', 0):.4f} "
            f"| {entry.get('change', 0):,} |"
        )

    # Summary
    recent_mspr = [e.get("mspr", 0) for e in entries[-3:]]
    avg_mspr = sum(recent_mspr) / len(recent_mspr) if recent_mspr else 0
    if avg_mspr > 0.1:
        signal = "**Strong insider buying** - bullish signal"
    elif avg_mspr > 0:
        signal = "**Mild insider buying** - slightly bullish"
    elif avg_mspr > -0.1:
        signal = "**Mild insider selling** - slightly bearish"
    else:
        signal = "**Strong insider selling** - bearish signal"

    lines.append("")
    lines.append(f"**3-month average MSPR**: {avg_mspr:.4f} -- {signal}")

    return "\n".join(lines)


def _fetch_recommendation_trends(ticker: str, api_key: str) -> str:
    """Fetch analyst recommendation trends (buy/hold/sell consensus)."""
    params = {
        "symbol": ticker,
        "token": api_key,
    }

    try:
        resp = requests.get(f"{_BASE_URL}/stock/recommendation", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return f"Error fetching recommendation trends: {e}"

    if not data:
        return "No analyst recommendation data available."

    lines = [
        "### Analyst Recommendation Trends",
        "",
        "| Period | Strong Buy | Buy | Hold | Sell | Strong Sell | Consensus |",
        "|--------|------------|-----|------|------|-------------|-----------|",
    ]

    for entry in data[:4]:  # Last 4 months
        sb = entry.get("strongBuy", 0)
        b = entry.get("buy", 0)
        h = entry.get("hold", 0)
        s = entry.get("sell", 0)
        ss = entry.get("strongSell", 0)
        total = sb + b + h + s + ss

        if total > 0:
            bullish_pct = ((sb + b) / total) * 100
            if bullish_pct >= 70:
                consensus = "Bullish"
            elif bullish_pct >= 50:
                consensus = "Moderately Bullish"
            elif bullish_pct >= 30:
                consensus = "Mixed"
            else:
                consensus = "Bearish"
        else:
            consensus = "N/A"

        lines.append(
            f"| {entry.get('period', 'N/A')} "
            f"| {sb} | {b} | {h} | {s} | {ss} | {consensus} |"
        )

    # Current consensus summary
    if data:
        latest = data[0]
        sb = latest.get("strongBuy", 0)
        b = latest.get("buy", 0)
        h = latest.get("hold", 0)
        s = latest.get("sell", 0)
        ss = latest.get("strongSell", 0)
        total = sb + b + h + s + ss
        if total > 0:
            lines.append("")
            lines.append(
                f"**Current consensus**: {sb + b} bullish / {h} hold / {s + ss} bearish "
                f"out of {total} analysts ({((sb + b) / total) * 100:.0f}% bullish)"
            )

    return "\n".join(lines)


def get_social_sentiment(ticker: str, curr_date: str, look_back_days: int = 7) -> str:
    """Fetch sentiment data from Finnhub for a given ticker.

    Combines insider sentiment (MSPR) and analyst recommendation trends
    to provide a comprehensive sentiment picture using Finnhub's free tier.

    Args:
        ticker: Stock ticker symbol (e.g. "AAPL", "NVDA").
        curr_date: Reference date in YYYY-MM-DD format.
        look_back_days: Number of days to look back (default 7, extended
            internally for monthly insider data).

    Returns:
        Formatted string with sentiment data suitable for LLM consumption.
    """
    api_key = _get_api_key()

    # Strip exchange suffix for Finnhub (e.g. "AAPL.TO" -> "AAPL")
    clean_ticker = ticker.upper().split(".")[0]

    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    # Insider sentiment is monthly, so look back 6 months minimum
    start_dt = end_dt - timedelta(days=180)

    lines = [
        f"## Sentiment & Consensus Report for {ticker}",
        f"**Analysis date**: {curr_date}",
        "",
    ]

    # Fetch insider sentiment
    insider_section = _fetch_insider_sentiment(
        clean_ticker, api_key, start_dt.strftime("%Y-%m-%d"), curr_date
    )
    lines.append(insider_section)
    lines.append("")

    # Fetch analyst recommendations
    recommendation_section = _fetch_recommendation_trends(clean_ticker, api_key)
    lines.append(recommendation_section)

    return "\n".join(lines)
