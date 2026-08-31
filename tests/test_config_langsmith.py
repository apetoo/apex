from apex import config


def test_load_maps_langsmith_yaml_to_official_environment_variables(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
langsmith:
  enabled: true
  api_key: local-test-key
  project: apex-local
  workspace_id: workspace-local
paths: {}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_WORKSPACE_ID", raising=False)

    loaded = config.load(str(config_path))

    assert loaded["langsmith"] == {
        "enabled": True,
        "api_key": "local-test-key",
        "project": "apex-local",
        "workspace_id": "workspace-local",
    }
    assert config.os.environ["LANGSMITH_TRACING"] == "true"
    assert config.os.environ["LANGSMITH_API_KEY"] == "local-test-key"
    assert config.os.environ["LANGSMITH_PROJECT"] == "apex-local"
    assert config.os.environ["LANGSMITH_WORKSPACE_ID"] == "workspace-local"


def test_load_preserves_environment_only_langsmith_configuration(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths: {}\n", encoding="utf-8")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "environment-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "environment-project")
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "environment-workspace")

    loaded = config.load(str(config_path))

    assert loaded["langsmith"] == {
        "enabled": True,
        "api_key": "environment-key",
        "project": "environment-project",
        "workspace_id": "environment-workspace",
    }
