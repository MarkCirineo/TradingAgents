"""StockTwits social sentiment provider.

Fetches recent posts from StockTwits' public API for a given ticker symbol.
No API key required.  Returns:
  - Code-computed sentiment breakdown (Bullish / Bearish / Neutral counts & ratio)
  - Watchlist popularity
  - A sample of recent posts so the LLM can read real retail trader commentary

Public endpoint: https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json
Rate limit: ~200 req/hour (IP-based, no auth).  We make 1 call per run.

Note: We use urllib instead of requests because Cloudflare's bot protection
on api.stocktwits.com fingerprints the TLS stack and blocks the `requests`
library (403 Forbidden), but allows urllib through.
"""

import json
import urllib.request
from datetime import datetime


_BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol"


def get_stocktwits_sentiment(ticker: str, curr_date: str, look_back_days: int = 7) -> str:
    """Fetch real social sentiment from StockTwits for a given ticker.

    Args:
        ticker: Stock ticker symbol (e.g. "NVDA", "AAPL").
        curr_date: Reference date in YYYY-MM-DD format (for report header).
        look_back_days: Unused (StockTwits returns most-recent posts).

    Returns:
        Formatted markdown string with sentiment data for LLM consumption.
    """
    clean_ticker = ticker.upper().split(".")[0]

    try:
        url = f"{_BASE_URL}/{clean_ticker}.json"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except Exception as e:
        return f"Error fetching StockTwits data for {ticker}: {e}"

    messages = data.get("messages", [])
    symbol_info = data.get("symbol", {})

    if not messages:
        return f"No StockTwits posts found for {ticker}."

    # ── Code-computed sentiment breakdown ────────────────────────────
    bullish = 0
    bearish = 0
    no_label = 0

    for msg in messages:
        sentiment = (
            msg.get("entities", {}).get("sentiment", {}) or {}
        ).get("basic")
        if sentiment == "Bullish":
            bullish += 1
        elif sentiment == "Bearish":
            bearish += 1
        else:
            no_label += 1

    total = len(messages)
    labeled = bullish + bearish
    bullish_pct = (bullish / labeled * 100) if labeled > 0 else 0
    bearish_pct = (bearish / labeled * 100) if labeled > 0 else 0

    # Determine overall mood
    if labeled == 0:
        mood = "Insufficient labeled posts to determine sentiment"
    elif bullish_pct >= 70:
        mood = "Strongly Bullish"
    elif bullish_pct >= 55:
        mood = "Moderately Bullish"
    elif bearish_pct >= 70:
        mood = "Strongly Bearish"
    elif bearish_pct >= 55:
        mood = "Moderately Bearish"
    else:
        mood = "Mixed / Neutral"

    # ── Symbol-level metadata ────────────────────────────────────────
    watchlist_count = symbol_info.get("watchlist_count", 0)
    watchlist_str = f"{watchlist_count:,}" if isinstance(watchlist_count, int) else str(watchlist_count)

    # ── Build the report ─────────────────────────────────────────────
    lines = [
        f"## StockTwits Social Sentiment Report for {clean_ticker}",
        f"**Analysis date**: {curr_date}",
        f"**Data source**: StockTwits public feed (most recent {total} posts)",
        "",
        "### Sentiment Breakdown",
        "",
        "These counts are computed from user-tagged sentiment labels on StockTwits posts.",
        "Each user can optionally tag their post as Bullish or Bearish when posting.",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| **Total posts analyzed** | {total} |",
        f"| **Bullish posts** | {bullish} ({bullish_pct:.0f}% of labeled) |",
        f"| **Bearish posts** | {bearish} ({bearish_pct:.0f}% of labeled) |",
        f"| **Unlabeled posts** | {no_label} (no sentiment tag) |",
        f"| **Overall mood** | **{mood}** |",
        "",
        "### Popularity Metrics",
        "",
        f"- **Watchlist count**: {watchlist_str} users are watching {clean_ticker} on StockTwits",
        f"  (This shows how many retail investors actively track this stock.)",
        "",
    ]

    # ── Sample posts for LLM to read ─────────────────────────────────
    # Include up to 10 posts with their sentiment label so the LLM can
    # identify themes, catalysts, and the general "mood" of retail traders.
    lines.append("### Sample Recent Posts")
    lines.append("")
    lines.append(
        "Below are recent StockTwits posts for context. Use these to identify "
        "key themes, catalysts, and retail trader sentiment beyond the numbers."
    )
    lines.append("")

    sample_count = 0
    for msg in messages:
        if sample_count >= 10:
            break

        body = msg.get("body", "").strip()
        if not body or len(body) < 10:
            continue  # Skip empty or trivial posts

        # Clean up the body — truncate very long posts
        if len(body) > 300:
            body = body[:297] + "..."

        sentiment_data = (
            msg.get("entities", {}).get("sentiment", {}) or {}
        )
        label = sentiment_data.get("basic", "No label")
        username = msg.get("user", {}).get("username", "anonymous")
        created = msg.get("created_at", "")

        # Format timestamp nicely if available
        time_str = ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                time_str = created

        lines.append(f"**@{username}** ({label}) — {time_str}")
        lines.append(f"> {body}")
        lines.append("")
        sample_count += 1

    if sample_count == 0:
        lines.append("*No posts with sufficient content to display.*")
        lines.append("")

    return "\n".join(lines)
