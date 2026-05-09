"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

import functools
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm, config: Optional[Dict[str, Any]] = None):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    # In daemon mode, append the swing-trading entry criteria to the
    # system prompt so the LLM evaluates setups through the A+ checklist.
    # In CLI mode (default), the trader operates exactly as before.
    _strategy_overlay = ""
    if config and config.get("trading_mode") == "daemon":
        from tradingagents.strategies.swing_playbook import get_entry_criteria_prompt
        _strategy_overlay = "\n\n" + get_entry_criteria_prompt()

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        investment_plan = state["investment_plan"]

        system_content = (
            "You are a trading agent analyzing market data to make investment decisions. "
            "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
            "Anchor your reasoning in the analysts' reports and the research plan."
        )
        # Append swing strategy overlay when in daemon mode
        system_content += _strategy_overlay

        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": (
                    f"Based on a comprehensive analysis by a team of analysts, here is an investment "
                    f"plan tailored for {company_name}. {instrument_context} This plan incorporates "
                    f"insights from current technical market trends, macroeconomic indicators, and "
                    f"social media sentiment. Use this plan as a foundation for evaluating your next "
                    f"trading decision.\n\nProposed Investment Plan: {investment_plan}\n\n"
                    f"Leverage these insights to make an informed and strategic decision."
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")

