# Foundry prompt-agent sample

This repository includes reference pipelines and an executable GitHub Actions
workflow for the ordinary prompt agent in `agent.yaml`.

- Keep credentials out of source; use Azure OIDC and GitHub environments.
- Preserve immutable release definitions across Dev, Test, and Production.
- Invoke explicit agent versions, never an implicit latest version.
- Validate with `python -m pytest`, `python -m ruff check .`, and
  `python -m bandit -r src -ll`.
- Do not describe the deterministic sample evaluations as production safety or
  hallucination measurement.

If you are in VS Code, read the vscode-microsoft-foundry skill first.
