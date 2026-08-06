"""Gemini AI consultation — second opinion for deep legal/research questions."""

import logging

import httpx

from goodman.config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def consult(question: str, system_prompt: str = "", timeout: int = 120) -> str | None:
    """Ask Gemini for analysis on a question. Returns response text or None.

    Used as a second-opinion consultant alongside Claude for deep
    legal, tax, and regulatory questions.
    """
    if not GEMINI_API_KEY:
        logger.debug("Gemini: no API key configured, skipping")
        return None

    try:
        contents = []
        if system_prompt:
            contents.append({
                "role": "user",
                "parts": [{"text": f"[System instructions]: {system_prompt}"}],
            })
            contents.append({
                "role": "model",
                "parts": [{"text": "Understood. I will follow these instructions."}],
            })

        contents.append({
            "role": "user",
            "parts": [{"text": question}],
        })

        resp = httpx.post(
            f"{API_BASE}/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": 16384,
                    "temperature": 0.3,
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract text from response
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "\n".join(p.get("text", "") for p in parts if p.get("text"))
            if text:
                logger.info("Gemini consultation: %d chars response", len(text))
                return text

        logger.warning("Gemini: empty response")
        return None

    except Exception as e:
        logger.error("Gemini consultation failed: %s", e)
        return None
