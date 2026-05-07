import logging

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_indicators,
    get_language_instruction,
    get_stock_data,
)
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

# Curated set of indicators that provide complementary insights without
# redundancy.  Used by the prefetch path so every run gets consistent,
# complete technical data regardless of the LLM's tool-calling ability.
_DEFAULT_INDICATORS = [
    "macd", "macds", "macdh",  # trend / momentum
    "rsi",                      # momentum
    "boll", "boll_ub", "boll_lb",  # volatility
    "atr",                      # volatility
    "vwma",                     # volume
    "close_50_sma", "close_200_sma",  # moving averages
]


def _prefetch_indicators(symbol: str, curr_date: str, look_back_days: int = 30) -> str:
    """Fetch all default indicators via code (no LLM tool calls needed).

    Returns a single formatted string with all indicator data, ready to be
    injected into the system prompt.
    """
    sections = []
    for indicator in _DEFAULT_INDICATORS:
        try:
            data = route_to_vendor("get_indicators", symbol, indicator, curr_date, look_back_days)
            sections.append(data)
        except Exception as e:
            logger.warning("Failed to prefetch indicator %s for %s: %s", indicator, symbol, e)
            sections.append(f"## {indicator}: Error fetching data — {e}")
    return "\n\n".join(sections)


def _prefetch_stock_data(symbol: str, curr_date: str) -> str:
    """Fetch recent OHLCV data via code.

    Only fetches the last 90 days to keep the prompt size manageable.
    The LLM-driven path fetches from 2020 which is unnecessarily large
    for technical analysis.
    """
    from datetime import datetime, timedelta
    end = datetime.strptime(curr_date, "%Y-%m-%d")
    start = end - timedelta(days=90)
    try:
        return route_to_vendor(
            "get_stock_data", symbol,
            start.strftime("%Y-%m-%d"), curr_date,
        )
    except Exception as e:
        logger.warning("Failed to prefetch stock data for %s: %s", symbol, e)
        return f"Error fetching stock data: {e}"


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        symbol = state["company_of_interest"]
        instrument_context = build_instrument_context(symbol)
        config = get_config()

        # ── Prefetch path: fetch all data via code, LLM only analyzes ────
        if config.get("prefetch_market_indicators", True):
            return _prefetch_path(llm, state, symbol, current_date, instrument_context)

        # ── Original path: LLM drives tool calls autonomously ────────────
        return _tool_call_path(llm, state, current_date, instrument_context)

    return market_analyst_node


def _prefetch_path(llm, state, symbol, current_date, instrument_context):
    """Pre-fetch all data and ask the LLM to analyze it (no tool calling)."""
    logger.info("Market Analyst: prefetching indicators for %s", symbol)

    stock_data = _prefetch_stock_data(symbol, current_date)
    indicator_data = _prefetch_indicators(symbol, current_date)

    system_message = (
        """You are a trading assistant tasked with analyzing financial markets. You have been provided with recent OHLCV price data and a comprehensive set of technical indicators for the instrument below.

Your task is to write a **detailed, nuanced technical analysis report** based on this data. Provide specific, actionable insights with supporting evidence to help traders make informed decisions. Cover:

1. **Trend Analysis** — What do the moving averages (SMA 50/200) and MACD tell us about the current trend direction and momentum?
2. **Momentum Assessment** — What does RSI indicate about overbought/oversold conditions? Any divergence signals?
3. **Volatility Analysis** — What do Bollinger Bands and ATR reveal about current volatility and potential breakout/reversal zones?
4. **Volume Confirmation** — Does VWMA confirm or diverge from the price trend?
5. **Overall Recommendation** — Based on your analysis, provide a clear BUY/HOLD/SELL recommendation with reasoning.

Make sure to append a Markdown table at the end of the report to organize key points, organized and easy to read."""
        + get_language_instruction()
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "{system_message}\n\nFor your reference, the current date is {current_date}. {instrument_context}"),
            ("human", "Here is the recent price data:\n\n{stock_data}\n\nHere are the technical indicators:\n\n{indicator_data}\n\nPlease analyze this data and write your report."),
        ]
    )

    prompt = prompt.partial(
        system_message=system_message,
        current_date=current_date,
        instrument_context=instrument_context,
    )

    # No tool binding needed — the LLM just reads and writes
    chain = prompt | llm
    result = chain.invoke({
        "stock_data": stock_data,
        "indicator_data": indicator_data,
    })

    report = result.content if hasattr(result, "content") else str(result)

    return {
        "messages": [result],
        "market_report": report,
    }


def _tool_call_path(llm, state, current_date, instrument_context):
    """Original path: LLM uses tool calls to fetch data autonomously."""
    tools = [
        get_stock_data,
        get_indicators,
    ]

    system_message = (
        """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names. Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."""
        + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
        + get_language_instruction()
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a helpful AI assistant, collaborating with other assistants."
                " Use the provided tools to progress towards answering the question."
                " If you are unable to fully answer, that's OK; another assistant with different tools"
                " will help where you left off. Execute what you can to make progress."
                " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                " You have access to the following tools: {tool_names}.\n{system_message}"
                "For your reference, the current date is {current_date}. {instrument_context}",
            ),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )

    prompt = prompt.partial(system_message=system_message)
    prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
    prompt = prompt.partial(current_date=current_date)
    prompt = prompt.partial(instrument_context=instrument_context)

    chain = prompt | llm.bind_tools(tools)

    result = chain.invoke(state["messages"])

    report = ""

    if len(result.tool_calls) == 0:
        report = result.content

    return {
        "messages": [result],
        "market_report": report,
    }
