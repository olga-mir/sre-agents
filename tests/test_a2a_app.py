"""Smoke-test that the Agent Runtime A2A object is constructible."""

from e2e_agent.agent.a2a_app import a2a_agent


def test_a2a_agent_registers_message_send():
    ops = a2a_agent.register_operations()
    assert "on_message_send" in ops.get("a2a_extension", [])
    assert a2a_agent.agent_card.name == "e2e-agent"
