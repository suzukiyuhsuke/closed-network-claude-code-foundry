import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from azure.core.credentials import AccessToken


def test_smoke_uses_registered_pathless_redirect(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("smoke_test", root / "scripts" / "smoke_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    credential = MagicMock()
    credential.return_value.__enter__.return_value.get_token.return_value = AccessToken(
        "fake-token-for-local-test", 4102444800
    )
    verify = AsyncMock()
    monkeypatch.setattr(module, "InteractiveBrowserCredential", credential)
    monkeypatch.setattr(module, "verify", verify)
    monkeypatch.setattr("sys.argv", [
        "smoke_test.py", "--url", "https://example.test/mcp",
        "--tenant-id", "test-tenant", "--client-id", "test-client",
        "--api-client-id", "test-api", "--query", "test query",
    ])

    module.main()

    credential.assert_called_once_with(
        tenant_id="test-tenant", client_id="test-client", redirect_uri="http://localhost:8765"
    )
    credential.return_value.__enter__.return_value.get_token.assert_called_once_with(
        "api://test-api/search"
    )
    verify.assert_awaited_once_with(
        "https://example.test/mcp", "fake-token-for-local-test", "test query"
    )
    registration = (root / "scripts" / "New-EntraApps.ps1").read_text(encoding="utf-8")
    assert "'http://localhost:8765'" in registration
    assert "'http://localhost:8765/callback'" in registration
