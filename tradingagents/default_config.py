import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings -- override via env vars for the daemon (no interactive prompts)
    # Set LLM_PROVIDER=nvidia, DEEP_THINK_LLM=..., QUICK_THINK_LLM=... in .env
    "llm_provider": os.getenv("LLM_PROVIDER", "openai"),
    "deep_think_llm": os.getenv("DEEP_THINK_LLM", "gpt-5.4"),
    "quick_think_llm": os.getenv("QUICK_THINK_LLM", "gpt-5.4-mini"),
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": os.getenv("BACKEND_URL", None),
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # When True, the market analyst pre-fetches all technical indicators
    # via code instead of relying on the LLM to make multi-step tool calls.
    # Essential for models with weak tool-calling (e.g. Llama on NVIDIA NIM).
    # When False, the LLM decides which indicators to fetch autonomously.
    "prefetch_market_indicators": True,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
        "social_sentiment": "finnhub",       # Options: finnhub (requires FINNHUB_API_KEY)
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # -----------------------------------------------------------------------
    # Alpaca paper trading integration
    # -----------------------------------------------------------------------
    # Trading mode: "cli" (default, one-shot analysis) or "daemon" (always-on)
    "trading_mode": "cli",
    # Pipeline mode: "full" (LLM-gated) or "quant" (quantitative only, skip LLM)
    # Use for A/B testing: run two daemon instances with different modes.
    "pipeline_mode": os.getenv("PIPELINE_MODE", "full"),
    # Alpaca configuration
    "alpaca_paper": True,
    # Trading schedule (Eastern Time)
    "trading_schedule": {
        "pre_market": "08:00",              # Screener + regime check
        "analyze": "08:05",                 # LLM/quant pipeline (runs before open)
        "entry_window": "09:45",            # ORH/ORL buy-stop orders
        "midday_check": "12:00",            # Day 3 trims, parabolic exits
        "eod_check": "15:45",               # Day 1 red close, trailing SMA
        "post_market": "16:15",             # Daily snapshot + summary
    },
    # Guardrails -- pre-trade safety checks
    "guardrails": {
        "max_position_pct": 0.10,           # 10% of portfolio per position
        "max_exposure_pct": 0.60,           # 60% total invested
        "max_daily_loss_pct": 0.03,         # -3% daily drawdown halt
        "max_risk_per_trade_pct": 0.005,    # 0.5% risk per trade (hard cap)
        "target_risk_per_trade_pct": 0.0035,  # 0.35% risk per trade (default)
        "max_concurrent_positions": 6,
        "min_dollar_volume": 50_000_000,    # $50M avg daily dollar volume
    },
    # Ticker screening
    "screening": {
        "source": os.getenv("SCREENING_SOURCE", "alpaca"),  # alpaca, hybrid, watchlist
        "watchlist": [],                    # Manual ticker list
        "max_candidates": int(os.getenv("MAX_CANDIDATES", "35")),
        "max_pipeline_runs": int(os.getenv("MAX_PIPELINE_RUNS", "15")),
        "max_workers": int(os.getenv("MAX_WORKERS", "2")),
    },
    # Swing trading strategy parameters (distilled from expert document)
    "swing_strategy": {
        # Stock selection thresholds
        "min_prior_uptrend_pct": 0.30,      # 30% prior move minimum
        "min_rs_percentile": 0.85,          # Top 15% relative strength (reference)
        "min_rs_outperformance": 0.05,      # 5% above SPY over 20 days (enforced)
        "min_dollar_volume": 50_000_000,    # $50M avg daily dollar volume
        "min_adr_pct": 0.04,                # 4% average daily range
        "min_price": 5.0,                   # Minimum stock price
        "max_price": 500.0,                 # Maximum stock price
        # Entry method
        "orh_window_minutes": 15,           # Opening range window (9:30-9:45)
        # Exit rules
        "day1_red_close_exit": True,        # Exit if Day 1 closes red
        "partial_profit_day": 3,            # Sell 50% on Day 3
        "partial_profit_pct": 0.50,         # Sell 50% of position
        "trailing_ma_period": 10,           # 10-day SMA trail
        "trailing_ma_exit_on": "close",     # Exit on close below, not intraday
        "max_extension_adr_multiple": 7,    # Tighten stop if > 7x ADR above 50 SMA
        "soft_backstop_days": 30,           # Flag for review after 30 calendar days
    },
}
