from app.config import Settings


def test_generic_debug_environment_variable_does_not_pollute_settings(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "release")
    monkeypatch.delenv("AIOPS_DEBUG", raising=False)

    settings = Settings(_env_file=None)

    assert settings.debug is False


def test_aiops_debug_accepts_named_environment_modes(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DEBUG", "development")

    settings = Settings(_env_file=None)

    assert settings.debug is True
