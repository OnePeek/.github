# Contributing to OnePeek

Every OnePeek repo follows the engineering standards in `onepeek-docs` (https://github.com/OnePeek/onepeek-docs/blob/main/standards/engineering-standards.md). Each repo's `AGENTS.md` adds what is specific to it. Both apply to people and AI coding agents alike.

0. Clone the repos side by side in one folder, then run `python3 .github/workspace/setup.py` there once. Claude Code then has every repo's skills and agents even when started in that folder (`.github/workspace/`).
1. Start from the docs: a behaviour, API, event, MCP tool, data or infrastructure change begins as a pull request in `onepeek-docs`.
2. Branch from `dev` as `<type>/<short-topic>` (in `onepeek-docs` and `marketing-website`, from `main`), and use Conventional Commits. Branches, merges, CI and releases follow `standards/branching-and-delivery.md`.
3. Open a pull request into `dev` with the template. Before launch, review is optional and CodeRabbit's review is advisory; a Terraform plan that deletes or replaces a network, security, data or key resource still waits for the founder.
