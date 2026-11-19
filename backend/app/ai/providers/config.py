"""Create the configured provider for the CLI and evaluation runner."""
import os

from app.ai.providers.gemini_provider import GeminiExtractionProvider
from app.ai.providers.openai_provider import OpenAIExtractionProvider


def make_provider():
    name = os.getenv("AI_PROVIDER", "openai").strip().lower()
    if name == "gemini":
        return GeminiExtractionProvider(
            model_name=os.getenv("GEMINI_MODEL", ""), api_key=os.getenv("GEMINI_API_KEY", ""),
            timeout_seconds=float(os.getenv("GEMINI_TIMEOUT_SECONDS", "30")),
            max_output_tokens=int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2048")),
        )
    if name == "openai":
        return OpenAIExtractionProvider(
            model_name=os.getenv("OPENAI_MODEL", ""), api_key=os.getenv("OPENAI_API_KEY", ""),
            timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30")),
            max_output_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "1000")),
        )
    raise ValueError("AI_PROVIDER must be openai or gemini")
