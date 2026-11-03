from core.config import Settings


def test_model_name_whitespace_is_stripped():
    s = Settings(_env_file=None, llm_model="  qwen2.5:7b ", llm_provider=" ollama ")
    assert s.llm_model == "qwen2.5:7b"
    assert s.llm_provider == "ollama"


def test_no_secret_defaults(monkeypatch):
    for name in ("TAVILY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.tavily_api_key == ""
    assert s.openai_api_key == ""


def test_code_runs_in_docker_unless_you_choose_otherwise():
    s = Settings(_env_file=None)
    assert s.code_exec_backend == "docker"
    assert Settings(_env_file=None, code_exec_backend=" local ").code_exec_backend == "local"