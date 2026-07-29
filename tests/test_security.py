from starlette.requests import Request

from app.config import config
from app.security import required_role, resolve_identity, role_allows, scoped_session_id


def _request(path: str = "/api/chat", method: str = "GET", api_key: str = "") -> Request:
    headers = []
    if api_key:
        headers.append((b"x-api-key", api_key.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
            "scheme": "http",
        }
    )


def test_config_parses_security_settings(monkeypatch) -> None:
    monkeypatch.setattr(config, "api_keys", "read-secret:viewer,ops-secret:operator,bad")
    monkeypatch.setattr(config, "cors_origins", "http://localhost:9900,http://localhost:9900")
    assert config.api_key_roles == {"read-secret": "viewer", "ops-secret": "operator"}
    assert config.cors_origin_list == ["http://localhost:9900"]


def test_identity_and_session_are_scoped_to_key(monkeypatch) -> None:
    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "api_keys", "read-secret:viewer")
    request = _request(api_key="read-secret")
    identity = resolve_identity(request)
    assert identity is not None
    assert identity.role == "viewer"
    request.state.identity = identity
    assert scoped_session_id(request, "incident-1").endswith(":incident-1")
    assert scoped_session_id(request, "incident-1") != "incident-1"


def test_role_matrix_protects_mutations_and_admin_routes() -> None:
    assert required_role("/api/metrics/tools", "GET") == "viewer"
    assert required_role("/api/chat", "POST") == "operator"
    assert required_role("/api/upload", "POST") == "admin"
    assert role_allows("admin", "operator")
    assert not role_allows("viewer", "operator")
