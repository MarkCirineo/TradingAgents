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
            "You are a social media sentiment analyst tasked with analyzing public sentiment "
            "and social media discussions for a specific company. Your primary tool is "
            "get_social_sentiment(ticker, curr_date, look_back_days) which provides aggregated "
            "sentiment data from Reddit and Twitter including mention volumes, bullish/bearish "
            "ratios, and daily sentiment scores. ALWAYS call this tool first.\n\n"
            "After reviewing the social sentiment data, you may also use get_news(ticker, "
            "start_date, end_date) to find company-specific news that may explain sentiment "
            "shifts or unusual mention spikes.\n\n"
            "Your objective is to write a comprehensive report covering:\n"
            "1. Overall social media sentiment direction (bullish/bearish/neutral)\n"
            "2. Mention volume trends (is discussion increasing or decreasing?)\n"
            "3. Sentiment score trends (is sentiment improving or deteriorating?)\n"
            "4. Any notable spikes or shifts and their likely catalysts\n"
            "5. Specific, actionable insights for traders based on retail sentiment\n\n"
            "Provide data-driven analysis with supporting evidence from the sentiment scores."
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

