"""Quick smoke test: verify NVIDIA NIM models respond via the API.

Usage:
    python scripts/test_nim_models.py
"""

import os
import sys
import time

# Load .env
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

BASE_URL = os.getenv("BACKEND_URL", "https://integrate.api.nvidia.com/v1")
API_KEY = os.getenv("NVIDIA_API_KEY", "")

DEEP = os.getenv("DEEP_THINK_LLM", "mistralai/mistral-nemotron")
QUICK = os.getenv("QUICK_THINK_LLM", "meta/llama-4-maverick-17b-128e-instruct")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

PROMPT = "You are a stock analyst. In one sentence, what is the most important thing to check before buying a breakout stock?"


def test_model(label: str, model: str):
    print(f"\n{'='*60}")
    print(f"Testing {label}: {model}")
    print(f"{'='*60}")
    try:
        start = time.time()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROMPT}],
            max_tokens=150,
            timeout=30,
        )
        elapsed = time.time() - start
        content = resp.choices[0].message.content.strip()
        print(f"  OK Response ({elapsed:.1f}s): {content[:200]}")
        return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


if __name__ == "__main__":
    if not API_KEY:
        print("ERROR: NVIDIA_API_KEY not set")
        sys.exit(1)

    print(f"Endpoint: {BASE_URL}")
    print(f"Deep model: {DEEP}")
    print(f"Quick model: {QUICK}")

    r1 = test_model("DEEP_THINK", DEEP)
    r2 = test_model("QUICK_THINK", QUICK)

    print(f"\n{'='*60}")
    if r1 and r2:
        print("Both models OK — safe to deploy.")
    else:
        print("One or more models FAILED — check above.")
    print(f"{'='*60}")
