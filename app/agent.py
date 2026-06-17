import json
import httpx
from app.config import settings
from app.tools import TOOLS, execute_tool


SYSTEM_PROMPT = """You are Hermes, a powerful task automation agent.
You help users accomplish tasks by using available tools.
Always think step-by-step and use tools when needed to get accurate, real-time information.
Be concise, helpful, and action-oriented."""


async def run_agent(user_message: str, history: list[dict] = None) -> dict:
    """
    Run the Hermes agent with tool-calling loop.
    Returns final response and updated history.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if history:
        messages.extend(history)

    messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://hermes-agent.local",
        "X-Title": "Hermes Agent",
    }

    max_iterations = 10  # prevent infinite loops

    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(max_iterations):
            payload = {
                "model": settings.openrouter_model,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
            }

            response = await client.post(
                f"{settings.openrouter_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            message = data["choices"][0]["message"]
            messages.append(message)

            # No tool calls — we have the final answer
            if not message.get("tool_calls"):
                return {
                    "response": message.get("content") or "",
                    "history": messages[1:],  # exclude system prompt
                    "usage": data.get("usage", {}),
                }

            # Execute tool calls
            for tool_call in message["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_args = json.loads(tool_call["function"]["arguments"])

                tool_result = await execute_tool(tool_name, tool_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": str(tool_result),
                })

    return {
        "response": "Max iterations reached. Please try a simpler task.",
        "history": messages[1:],
        "usage": {},
    }
