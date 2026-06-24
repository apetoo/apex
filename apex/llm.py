"""
Unified LLM client for Apex. Wraps OpenAI-compatible APIs (DeepSeek, Qwen, etc.).

Usage:
    from apex.llm import chat, chat_stream

    # Non-streaming with tools
    reply = chat(messages=[...], tools=[...])

    # Streaming (no tools — streaming + function calling is tricky)
    for chunk in chat_stream(messages=[...]):
        print(chunk, end="")
"""

from openai import OpenAI

from apex import config as _cfg


def _get_client() -> OpenAI:
    conf = _cfg.get()
    return OpenAI(
        api_key=conf["deepseek"]["api_key"],
        base_url=conf["deepseek"].get("base_url", "https://api.deepseek.com"),
    )


def _get_model() -> str:
    return _cfg.get()["deepseek"]["model"]


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    model: str | None = None,
    extra_body: dict | None = None,
) -> dict:
    """Single-turn chat completion.

    Returns {"content": str, "tool_calls": list|None}.
    Each tool_call: {id, name, arguments: str}.
    """
    client = _get_client()
    kwargs: dict = dict(
        model=model or _get_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    if extra_body:
        kwargs["extra_body"] = extra_body

    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    result: dict = {"content": msg.content or "", "tool_calls": None}
    if msg.tool_calls:
        result["tool_calls"] = [
            {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in msg.tool_calls
        ]
    return result


def chat_stream(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    model: str | None = None,
    extra_body: dict | None = None,
):
    """Streaming chat completion. Yields content chunks (str)."""
    client = _get_client()
    kwargs: dict = dict(
        model=model or _get_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    if extra_body:
        kwargs["extra_body"] = extra_body

    stream = client.chat.completions.create(**kwargs)
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content
