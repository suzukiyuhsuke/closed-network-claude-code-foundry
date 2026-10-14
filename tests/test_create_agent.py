import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from azure.ai.projects.models import PromptAgentDefinition, WebSearchTool
import pytest


@pytest.mark.parametrize("from_version", [None, "1"])
def test_create_agent_preserves_source_when_switching_model(monkeypatch, from_version):
    path = Path(__file__).resolve().parents[1] / "scripts" / "create_agent.py"
    spec = importlib.util.spec_from_file_location("create_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = SimpleNamespace(
        definition=PromptAgentDefinition(
            model="original-model", instructions="Keep these instructions.",
            tools=[WebSearchTool()],
        ),
        description="Keep this description.",
        metadata={"purpose": "search"},
    )
    original = source.definition.as_dict()
    project_factory = MagicMock()
    project = project_factory.return_value.__enter__.return_value
    project.agents.get_version.return_value = source
    project.agents.create_version.return_value = SimpleNamespace(name="search", version="2")
    monkeypatch.setattr(module, "AIProjectClient", project_factory)
    monkeypatch.setattr(module, "AzureCliCredential", MagicMock())
    argv = [
        "create_agent.py", "--endpoint", "https://example.test/api/projects/test",
        "--name", "search", "--model", "gpt-5-mini",
    ]
    if from_version:
        argv.extend(["--from-version", from_version])
    monkeypatch.setattr("sys.argv", argv)

    module.main()

    kwargs = project.agents.create_version.call_args.kwargs
    assert kwargs["agent_name"] == "search"
    assert kwargs["definition"].model == "gpt-5-mini"
    if from_version:
        project.agents.get_version.assert_called_once_with(agent_name="search", agent_version="1")
        assert kwargs["definition"].as_dict() == {**original, "model": "gpt-5-mini"}
        assert source.definition.as_dict() == original
        assert kwargs["metadata"] == source.metadata
        assert kwargs["metadata"] is not source.metadata
        assert kwargs["description"] == source.description
    else:
        project.agents.get_version.assert_not_called()
        assert kwargs["definition"].instructions == module.INSTRUCTIONS
        assert kwargs["definition"].as_dict()["tools"] == [{"type": "web_search"}]
