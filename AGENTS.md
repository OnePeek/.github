# AGENTS.md: .github

OnePeek's organisation-wide GitHub files: the pull request template, the contributing and security guides, and the organisation profile. Every OnePeek repo uses these unless it has its own copy. This repo is public.

## Start here
- For any detail (what to build, how it behaves, names, data, limits), look in `onepeek-docs` first: the sibling folder `../onepeek-docs` or https://github.com/OnePeek/onepeek-docs. Begin with `standards/engineering-standards.md` and `standards/repo-map.md`, then grep and read only the files the task needs.
- If the docs are silent, unclear or disagree with these files, don't guess: ask, or open a docs pull request.

## Layout
- `PULL_REQUEST_TEMPLATE.md`: what changed, why, the docs link and how it was tested.
- `CONTRIBUTING.md` and `SECURITY.md`: how to contribute and how to report a vulnerability.
- `profile/`: the organisation's public profile page.

## Principles
- **Mirror the standards.** These files repeat `engineering-standards.md` in short form; when the standards change, these change with them.
- **Public by design.** Nothing here may hold a secret, an internal link or personal data.

These principles grow over time: when a review teaches us something, add it here in one line.

## Rules every OnePeek repo follows
Read `onepeek-docs/standards/engineering-standards.md` (https://github.com/OnePeek/onepeek-docs/blob/main/standards/engineering-standards.md) before changing anything. In short:
- **Names and language.** Canonical names from founder decision 97; plain English in anything a person reads; never name the prior internal product.
- **Review.** Every change goes through a pull request with one review. People and AI agents never commit, push, merge or deploy without an explicit instruction.
