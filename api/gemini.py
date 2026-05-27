"""
Клиент Gemini 2.0 Flash API (Google AI Studio).

Бесплатный тариф: 15 запросов/мин, 1500 запросов/день, 1M токенов/день.
Ключ получить: https://aistudio.google.com/app/apikey
"""
import aiohttp
import os

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/gemini-2.5-flash:generateContent"
)


async def ask(prompt: str, temperature: float = 0.2, max_tokens: int = 700) -> str:
    """
    Отправляет prompt в Gemini Flash, возвращает текст ответа.
    При ошибке возвращает строку начинающуюся с '❌'.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return (
            "❌ GEMINI_API_KEY не задан.\n\n"
            "Получи бесплатный ключ на:\n"
            "https://aistudio.google.com/app/apikey\n\n"
            "Добавь в .env файл:\nGEMINI_API_KEY=твой_ключ"
        )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":     temperature,
            "maxOutputTokens": max_tokens,
            "topP":            0.8,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{GEMINI_URL}?key={api_key}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json(content_type=None)

        # Проверяем ошибки API
        if "error" in data:
            err = data["error"]
            code = err.get("code", "")
            msg  = err.get("message", str(err))
            if code == 429:
                return "⏳ Слишком много запросов. Подожди минуту и попробуй снова."
            return f"❌ Gemini API: {msg}"

        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    except aiohttp.ClientTimeout:
        return "❌ Gemini не ответил за 30 секунд. Попробуй ещё раз."
    except Exception as e:
        return f"❌ Ошибка соединения: {e}"


async def chat(messages: list, max_tokens: int = 1500) -> str:
    """
    Мульти-턴 чат с Gemini.
    messages: [{"role": "user"|"model", "parts": [{"text": "..."}]}, ...]
    Возвращает текст последнего ответа модели.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "❌ GEMINI_API_KEY не задан."

    payload = {
        "contents": messages,
        "generationConfig": {
            "temperature":     0.35,
            "maxOutputTokens": max_tokens,
            "topP":            0.85,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{GEMINI_URL}?key={api_key}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as r:
                data = await r.json(content_type=None)

        if "error" in data:
            err  = data["error"]
            code = err.get("code", "")
            msg  = err.get("message", str(err))
            if code == 429:
                return "⏳ Слишком много запросов. Подожди минуту и попробуй снова."
            return f"❌ Gemini API: {msg}"

        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    except aiohttp.ClientTimeout:
        return "❌ Gemini не ответил за 45 секунд. Попробуй ещё раз."
    except Exception as e:
        return f"❌ Ошибка: {e}"
