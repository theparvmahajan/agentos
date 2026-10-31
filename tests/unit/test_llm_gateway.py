import pytest

from core.config import settings
from core.llm import LLMError, TaskType, gateway, generate
from core.llm.fake import FakeProvider


def test_generate_uses_default_model_and_returns_response(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "test-model")
    fake = FakeProvider(["answer"])

    resp = generate("question", provider=fake)

    assert resp.text == "answer"
    assert fake.calls[0]["model"] == "test-model"
    assert fake.calls[0]["json_mode"] is False


def test_task_type_can_override_model(monkeypatch):
    monkeypatch.setitem(gateway.MODEL_OVERRIDES, TaskType.CODING, "coder-model")
    fake = FakeProvider(["a", "b"])

    generate("write code", TaskType.CODING, provider=fake)
    generate("plan", TaskType.PLANNING, provider=fake)

    assert fake.calls[0]["model"] == "coder-model"
    assert fake.calls[1]["model"] == settings.llm_model


def test_system_and_json_mode_are_passed_through():
    fake = FakeProvider(["{}"])
    generate("p", system="sys", json_mode=True, provider=fake)
    assert fake.calls[0]["system"] == "sys"
    assert fake.calls[0]["json_mode"] is True


def test_fake_provider_runs_out_of_responses():
    fake = FakeProvider([])
    with pytest.raises(LLMError):
        generate("p", provider=fake)


def test_unknown_provider_name_raises(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nonsense")
    with pytest.raises(LLMError):
        gateway.get_provider()


def test_openai_compatible_requires_credentials(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(LLMError):
        gateway.get_provider()