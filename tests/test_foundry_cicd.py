import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openai import APIConnectionError

from foundry_cicd import (
    deploy,
    digest,
    enforce_gates,
    evaluate,
    invoke,
    load_config,
    load_release,
    project_client,
    score,
    summarize,
)


def test_config():
    assert load_config(Path("agent.yaml"))["kind"] == "prompt"


@pytest.mark.parametrize("text", ["", "[]", "null", "kind: hosted", "name: x"])
def test_invalid_config(tmp_path, text):
    path = tmp_path / "agent.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize(
    "text,valid,correct",
    [
        ('{"decision":"approve","reason":"All checks passed"}', True, True),
        ('{"decision":"block","reason":"Missing approval"}', True, False),
        ('{"decision":"approve","reason":""}', False, False),
        ('{"decision":"approve","reason":"ok","extra":true}', False, False),
        ("```json\n{}\n```", False, False),
        ("[]", False, False),
    ],
)
def test_score(text, valid, correct):
    assert score(text, "approve") == (valid, correct)


def test_p95_and_rates():
    rows = [
        {"latency_ms": index, "valid": index != 1, "correct": index > 2} for index in range(1, 21)
    ]
    assert summarize(rows) == {
        "invalid_output_rate": 0.05,
        "task_completion": 0.9,
        "latency_p95_ms": 19,
    }
    with pytest.raises(ValueError):
        summarize([])


@pytest.mark.parametrize(
    "key,value",
    [
        ("invalid_output_rate", 0.051),
        ("task_completion", 0.899),
        ("latency_p95_ms", 4001),
        ("latency_p95_ms", float("nan")),
        ("task_completion", True),
        ("task_completion", 2),
    ],
)
def test_gate_failures(key, value):
    metrics = {"invalid_output_rate": 0.05, "task_completion": 0.90, "latency_p95_ms": 4000}
    enforce_gates(metrics, 0.05, 0.90, 4000)
    metrics[key] = value
    with pytest.raises(ValueError, match="Quality gate failed"):
        enforce_gates(metrics, 0.05, 0.90, 4000)


def test_deploy_promotes_exact_definition(tmp_path):
    client = MagicMock()
    config = load_config(Path("agent.yaml"))
    client.deployments.list.return_value = [MagicMock(name="deployment")]
    client.deployments.list.return_value[0].name = config["model"]
    client.agents.create_version.return_value.name = config["name"]
    client.agents.create_version.return_value.version = "2"
    endpoint = "https://example.services.ai.azure.com/api/projects/test"
    path = tmp_path / "release.json"
    manifest = deploy(client, config, config["name"], endpoint, path)
    definition = client.agents.create_version.call_args.kwargs["definition"]
    assert definition.model == config["model"]
    assert definition.instructions == config["instructions"]
    assert definition.text.format.schema == config["response_schema"]
    assert load_release(path)["config_sha256"] == digest(config)
    manifest["config"]["instructions"] = "tampered"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="checksum"):
        load_release(path)


def test_invokes_exact_version():
    client = MagicMock()
    response = client.responses
    response.create.return_value.status = "completed"
    response.create.return_value.output_text = "reply"
    assert invoke(client, "agent", "42", "hello") == "reply"
    assert response.create.call_args.kwargs["extra_body"]["agent_reference"]["version"] == "42"
    response.create.return_value.status = "failed"
    with pytest.raises(RuntimeError):
        invoke(client, "agent", "42", "hello")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com/api/projects/dev",
        "http://example.services.ai.azure.com/api/projects/dev",
        "https://example.services.ai.azure.com",
    ],
)
def test_invalid_endpoint(endpoint):
    with pytest.raises(ValueError):
        project_client(endpoint)


def test_evaluation_api_failure_preserves_report(tmp_path):
    config = load_config(Path("agent.yaml"))
    client = MagicMock()
    remote = client.agents.get_version.return_value
    remote.definition.model = config["model"]
    remote.definition.instructions = config["instructions"]
    remote.definition.text.format.schema = config["response_schema"]
    openai = client.get_openai_client.return_value.__enter__.return_value
    openai.responses.create.side_effect = APIConnectionError(request=MagicMock())
    manifest = {
        "name": config["name"],
        "version": "1",
        "config": config,
        "config_sha256": digest(config),
    }
    output = tmp_path / "results.json"
    with pytest.raises(RuntimeError, match="report saved"):
        evaluate(client, manifest, Path("eval/datasets/golden_set.jsonl"), output)
    result = json.loads(output.read_text())
    assert len(result["cases"]) == 8
    assert result["metrics"]["task_completion"] == 0
    assert all("error" in row for row in result["cases"])


def test_evaluation_rejects_definition_drift(tmp_path):
    config = load_config(Path("agent.yaml"))
    client = MagicMock()
    client.agents.get_version.return_value.definition.model = "different-model"
    manifest = {"name": config["name"], "version": "1", "config": config}
    with pytest.raises(ValueError, match="differs"):
        evaluate(
            client, manifest, Path("eval/datasets/golden_set.jsonl"), tmp_path / "results.json"
        )
    client.get_openai_client.assert_not_called()
