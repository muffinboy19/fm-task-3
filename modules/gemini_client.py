"""
Google Gemini client with rotating API keys.
"""

import time
from typing import Optional

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

from modules.agent_logger import get_logger
from modules.config import get_env, get_gemini_api_keys

_MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
]


class GeminiLLMClient:
    def __init__(self, api_key: Optional[str] = None):
        if api_key:
            self.api_keys = [api_key]
        else:
            self.api_keys = get_gemini_api_keys()
        if not self.api_keys:
            raise ValueError(
                "Gemini API key required. Set GEMINI_API_KEY in .env."
            )
        self.model_name = get_env("GEMINI_MODEL", "gemini-2.5-flash")
        self._key_cursor = 0

    def _mask_key(self, api_key: str) -> str:
        return f"...{api_key[-4:]}" if len(api_key) >= 8 else "***"

    def _keys_round_robin(self) -> list[str]:
        n = len(self.api_keys)
        start = self._key_cursor % n
        self._key_cursor += 1
        return self.api_keys[start:] + self.api_keys[:start]

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        module: str = "llm",
    ) -> str:
        log = get_logger()
        log.info(f"Gemini keys in rotation: {len(self.api_keys)}")

        models_to_try = [self.model_name] + [
            m for m in _MODEL_FALLBACKS if m != self.model_name
        ]

        last_err = None
        for model_name in models_to_try:
            for api_key in self._keys_round_robin():
                genai.configure(api_key=api_key)
                try:
                    text, elapsed = self._call(model_name, system, user, max_tokens)
                    log.llm_call(
                        module=module,
                        model=model_name,
                        system_preview=system[:1500],
                        user_preview=user[:3000],
                        response=text,
                        duration_sec=elapsed,
                    )
                    return text
                except google_exceptions.NotFound as e:
                    last_err = e
                    log.warning(f"Model not found: {model_name}")
                    break
                except google_exceptions.ResourceExhausted as e:
                    last_err = e
                    log.warning(
                        f"Quota on key {self._mask_key(api_key)} — next key"
                    )
                except Exception as e:
                    last_err = e
                    log.warning(f"Gemini error: {e}")

        raise RuntimeError(
            f"Gemini API failed for all keys. Last error: {last_err}"
        ) from last_err

    def _call(self, model_name: str, system: str, user: str, max_tokens: int):
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system,
        )
        start = time.time()
        response = model.generate_content(
            user,
            generation_config=genai.GenerationConfig(max_output_tokens=max_tokens),
        )
        elapsed = time.time() - start
        text = response.text or ""
        if not text:
            raise RuntimeError(f"Empty response from {model_name}")
        return text, elapsed
