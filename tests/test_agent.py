import asyncio

import pytest

from app import agent
from app.agent import AgentTimeout, run_agent, run_agent_events


def _msg(content=None, tool_calls=None, usage=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"choices": [{"message": m}], "usage": usage or {}}


def _tc(i, name, args):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": args}}


def script(monkeypatch, responses, seen=None):
    it = iter(responses)

    async def fake_call(client, payload):
        if seen is not None:
            seen.append(payload)
        return next(it)

    monkeypatch.setattr(agent, "call_llm", fake_call)


@pytest.mark.asyncio
async def test_empty_arguments_and_bad_json_do_not_crash(monkeypatch):
    seen = []
    script(monkeypatch, [
        _msg(tool_calls=[_tc(1, "get_current_time", ""), _tc(2, "calculate", "{not json")],
             usage={"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001}),
        _msg("done", usage={"prompt_tokens": 30, "completion_tokens": 5, "cost": 0.002}),
    ], seen)
    final = await run_agent("what time")
    assert final["response"] == "done"
    assert final["tools_used"] == ["get_current_time", "calculate"]
    # Usage is summed across every LLM call, not just the last one.
    assert final["usage"] == {"prompt_tokens": 40, "completion_tokens": 7, "cost": pytest.approx(0.003)}
    tool_msgs = [m for m in seen[1]["messages"] if m["role"] == "tool"]
    assert "+08:00" in tool_msgs[0]["content"]
    assert "not valid JSON" in tool_msgs[1]["content"]
    assert all(m["content"].startswith("<tool_output") for m in tool_msgs)


@pytest.mark.asyncio
async def test_tools_run_in_parallel(monkeypatch):
    async def slow_tool(name, args):
        await asyncio.sleep(0.3)
        return "ok"

    monkeypatch.setattr(agent, "execute_tool", slow_tool)
    script(monkeypatch, [
        _msg(tool_calls=[_tc(i, "calculate", '{"expression":"1"}') for i in range(3)]),
        _msg("fin"),
    ])
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await run_agent("go")
    assert loop.time() - t0 < 0.8


@pytest.mark.asyncio
async def test_last_iteration_forces_text_answer(monkeypatch):
    monkeypatch.setattr(agent.settings, "max_iterations", 2)
    seen = []
    script(monkeypatch, [
        _msg(tool_calls=[_tc(1, "calculate", '{"expression":"1+1"}')]),
        _msg("forced answer"),
    ], seen)
    final = await run_agent("loop")
    assert [p["tool_choice"] for p in seen] == ["auto", "none"]
    assert final["response"] == "forced answer" and final["iterations"] == 2


@pytest.mark.asyncio
async def test_deadline(monkeypatch):
    monkeypatch.setattr(agent.settings, "agent_timeout_seconds", 0.2)

    async def hang(client, payload):
        await asyncio.sleep(5)

    monkeypatch.setattr(agent, "call_llm", hang)
    with pytest.raises(AgentTimeout):
        await run_agent("hang")


@pytest.mark.asyncio
async def test_events_order(monkeypatch):
    script(monkeypatch, [
        _msg(tool_calls=[_tc(1, "calculate", '{"expression":"2*3"}')]),
        _msg("6"),
    ])
    events = [e async for e in run_agent_events("2*3?")]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "final"]
    assert "6" in events[1]["preview"]
