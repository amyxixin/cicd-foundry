import argparse
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    TextResponseFormatJsonSchema,
)
from azure.identity import AzureCliCredential, DefaultAzureCredential
from openai import OpenAI, OpenAIError


def load_config(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    required = {"name", "kind", "model", "instructions", "response_schema"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("Config requires name, kind, model, instructions, and response_schema")
    if any(
        not isinstance(config[key], str) or not config[key].strip()
        for key in required - {"response_schema"}
    ):
        raise ValueError("Config values must be non-empty strings")
    if not isinstance(config["response_schema"], dict) or not config["response_schema"]:
        raise ValueError("response_schema must be a non-empty JSON schema object")
    if config["kind"] != "prompt":
        raise ValueError("Only ordinary prompt agents are supported")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", config["name"]):
        raise ValueError("Agent name must be 1-63 letters, numbers, underscores or hyphens")
    return config


def digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def project_client(endpoint: str) -> AIProjectClient:
    url = urlparse(endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".services.ai.azure.com")
        or not re.fullmatch(r"/api/projects/[^/]+", url.path)
        or url.query
        or url.fragment
        or url.username
        or url.port
    ):
        raise ValueError(
            "Use a Foundry project endpoint: https://<resource>/api/projects/<project>"
        )
    credential = (
        AzureCliCredential(process_timeout=60)
        if os.environ.get("FOUNDRY_USE_AZURE_CLI") == "1"
        else DefaultAzureCredential()
    )
    return AIProjectClient(endpoint=endpoint, credential=credential)


def deploy(
    client: AIProjectClient, config: dict[str, Any], name: str, endpoint: str, output: Path
) -> dict[str, Any]:
    if config["model"] not in {item.name for item in client.deployments.list()}:
        raise ValueError(f"Model deployment not found: {config['model']}")
    version = client.agents.create_version(
        agent_name=name,
        definition=PromptAgentDefinition(
            model=config["model"],
            instructions=config["instructions"],
            text=PromptAgentDefinitionTextOptions(
                format=TextResponseFormatJsonSchema(
                    name="release_decision", schema=config["response_schema"], strict=True
                )
            ),
        ),
        metadata={"config_sha256": digest(config)},
    )
    manifest = {
        "name": version.name,
        "version": version.version,
        "endpoint": endpoint,
        "config": config,
        "config_sha256": digest(config),
    }
    write_json(output, manifest)
    return manifest


def load_release(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if digest(manifest["config"]) != manifest["config_sha256"]:
        raise ValueError("Release configuration checksum mismatch")
    return manifest


def invoke(client: OpenAI, name: str, version: str, message: str) -> str:
    response = client.responses.create(
        input=message,
        extra_body={
            "agent_reference": {"name": name, "version": version, "type": "agent_reference"}
        },
    )
    if response.status != "completed" or not response.output_text.strip():
        raise RuntimeError(f"Agent response did not complete: {response.status}")
    return response.output_text


def score(text: str, expected: str) -> tuple[bool, bool]:
    try:
        response = json.loads(text)
    except json.JSONDecodeError:
        return False, False
    valid = (
        isinstance(response, dict)
        and set(response) == {"decision", "reason"}
        and response["decision"] in ("approve", "block")
        and isinstance(response["reason"], str)
        and bool(response["reason"].strip())
    )
    return valid, valid and response["decision"] == expected


def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        raise ValueError("Evaluation dataset cannot be empty")
    latencies = sorted(row["latency_ms"] for row in rows)
    return {
        "invalid_output_rate": sum(not row["valid"] for row in rows) / len(rows),
        "task_completion": sum(row["correct"] for row in rows) / len(rows),
        "latency_p95_ms": latencies[math.ceil(0.95 * len(rows)) - 1],
    }


def enforce_gates(
    metrics: dict[str, Any], max_invalid: float, min_completion: float, max_latency: float
) -> None:
    bounds = {
        "invalid_output_rate": (0, max_invalid),
        "task_completion": (min_completion, 1),
        "latency_p95_ms": (0, max_latency),
    }
    for key, (low, high) in bounds.items():
        value = metrics.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise ValueError(f"Quality gate failed: {key}={value!r}; required [{low}, {high}]")


def evaluate(
    client: AIProjectClient, manifest: dict[str, Any], dataset: Path, output: Path
) -> dict[str, Any]:
    cases = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        raise ValueError("Evaluation dataset cannot be empty")
    for case in cases:
        if (
            not isinstance(case.get("input"), str)
            or not case["input"].strip()
            or case.get("expected_decision") not in ("approve", "block")
        ):
            raise ValueError(f"Invalid evaluation case: {case!r}")
    remote = client.agents.get_version(manifest["name"], manifest["version"])
    if (
        remote.definition.model != manifest["config"]["model"]
        or remote.definition.instructions != manifest["config"]["instructions"]
        or remote.definition.text.format.schema != manifest["config"]["response_schema"]
    ):
        raise ValueError("Remote agent definition differs from release")
    rows = []
    with client.get_openai_client() as openai:
        for case in cases:
            started = time.perf_counter()
            try:
                text = invoke(openai, manifest["name"], manifest["version"], case["input"])
            except OpenAIError as error:
                rows.append(
                    {
                        **case,
                        "response": None,
                        "error": str(error),
                        "valid": False,
                        "correct": False,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                    }
                )
                continue
            latency = (time.perf_counter() - started) * 1000
            valid, correct = score(text, case["expected_decision"])
            rows.append(
                {
                    **case,
                    "response": text,
                    "valid": valid,
                    "correct": correct,
                    "latency_ms": latency,
                }
            )
    result = {
        "agent": {"name": manifest["name"], "version": manifest["version"]},
        "config_sha256": manifest["config_sha256"],
        "metrics": summarize(rows),
        "cases": rows,
    }
    write_json(output, result)
    errors = [row["error"] for row in rows if "error" in row]
    if errors:
        raise RuntimeError(f"Evaluation API failures; report saved to {output}: {errors}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Version, evaluate and promote a prompt agent")
    parser.add_argument("command", choices=["validate", "deploy", "evaluate", "gate", "chat"])
    parser.add_argument("--config", type=Path, default=Path("agent.yaml"))
    parser.add_argument("--endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT"))
    parser.add_argument("--name")
    parser.add_argument("--release", type=Path)
    parser.add_argument("--output", type=Path, default=Path("eval/results/release.json"))
    parser.add_argument("--dataset", type=Path, default=Path("eval/datasets/golden_set.jsonl"))
    parser.add_argument("--results", type=Path, default=Path("eval/results/results.json"))
    parser.add_argument("--message")
    parser.add_argument("--max-invalid-output", type=float, default=0.05)
    parser.add_argument("--min-task-completion", type=float, default=0.90)
    parser.add_argument("--max-latency-p95", type=float, default=4000)
    args = parser.parse_args()
    if args.command == "validate":
        load_config(args.config)
        print("Prompt agent configuration valid")
        return
    if args.command == "gate":
        result = json.loads(args.results.read_text(encoding="utf-8"))
        if not result.get("cases"):
            parser.error("Evaluation report must include non-empty cases")
        metrics = summarize(result["cases"])
        if metrics != result["metrics"]:
            parser.error("Evaluation metrics do not match case results")
        enforce_gates(
            metrics, args.max_invalid_output, args.min_task_completion, args.max_latency_p95
        )
        print(json.dumps(metrics))
        return
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    manifest = load_release(args.release) if args.release else None
    if args.command in ("evaluate", "chat") and manifest is None:
        parser.error("--release is required")
    with project_client(args.endpoint) as client:
        if args.command == "deploy":
            config = manifest["config"] if manifest else load_config(args.config)
            deployed = deploy(
                client, config, args.name or config["name"], args.endpoint, args.output
            )
            print(json.dumps({"name": deployed["name"], "version": deployed["version"]}))
        elif manifest is not None:
            if args.endpoint != manifest["endpoint"]:
                parser.error("Endpoint must match the selected release")
            if args.command == "evaluate":
                result = evaluate(client, manifest, args.dataset, args.results)
                print(json.dumps(result["metrics"]))
            else:
                if not args.message:
                    parser.error("--message is required")
                with client.get_openai_client() as openai:
                    print(invoke(openai, manifest["name"], manifest["version"], args.message))


if __name__ == "__main__":
    main()
