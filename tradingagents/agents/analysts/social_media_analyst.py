from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
    get_news,
    get_social_sentiment,
)
from tradingagents.dataflows.config import get_config


def create_social_media_analyst(llm):
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_social_sentiment,
            get_news,
        ]

        system_message = (
            "You are a sentiment analyst tasked with analyzing public sentiment "
            "and market consensus for a specific company. Your primary tool is "
            "get_social_sentiment(ticker, curr_date, look_back_days) which returns "
            "a combined report containing:\n\n"
            "1. **Professional Analyst Consensus** (from Finnhub): Wall Street analyst "
            "recommendation trends (Strong Buy/Buy/Hold/Sell/Strong Sell counts) and "
            "insider sentiment (MSPR - Monthly Share Purchase Ratio) when available.\n"
            "2. **Retail Trader Sentiment** (from StockTwits): Real social media posts "
            "from retail traders with user-tagged Bullish/Bearish labels, computed "
            "sentiment ratios, watchlist popularity, and sample posts.\n\n"
            "ALWAYS call this tool first.\n\n"
            "After reviewing the sentiment data, you may also use get_news(ticker, "
            "start_date, end_date) to find company-specific news that may explain "
            "sentiment shifts.\n\n"
            "Your objective is to write a comprehensive report covering:\n"
            "1. Professional analyst consensus (what do Wall Street analysts recommend?)\n"
            "2. Retail trader sentiment direction (bullish/bearish/neutral from StockTwits)\n"
            "3. Key themes from retail trader posts (what are people talking about?)\n"
            "4. Any divergence between professional and retail sentiment\n"
            "5. Insider activity signals (if MSPR data is available)\n"
            "6. Specific, actionable insights for traders\n\n"
            "IMPORTANT: Only reference data, statistics, and figures that appear in the "
            "tool output. Do not fabricate Reddit mention volumes, Twitter sentiment scores, "
            "or any social media statistics that were not returned by the tool. If a data "
            "source returned no data, say so explicitly rather than inventing numbers.\n\n"
            "Provide data-driven analysis with supporting evidence from the actual data returned."
            """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
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
            "sentiment_report": report,
        }

    return social_media_analyst_node

