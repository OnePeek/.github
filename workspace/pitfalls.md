# Pitfalls: read before you build or review

Every builder and reviewer reads this whole file before starting, after `onepeek-docs/standards/agent-operations.md` (their role's sections), and checks its work against both before reporting.
Each line is a mistake that already happened. When a review finds a new kind of mistake, add one line here in the
same pull request as the fix.

## Where you work
1. **Work from `origin/<base>` in your own git worktree.** The shared checkouts in the workspace folder may sit on old
   branches. On 2026-11-06 an agent read a stale backend branch and reported a setting the code no longer had. Run
   `git fetch` first. Never edit, stash or commit in a shared checkout.
2. **Never `commit -a`, `--no-verify` or a force push.** Push a detached worktree with `git push origin HEAD:<branch>`.
3. **Commit every file you changed.** On 2026-11-06 a builder pushed a commit holding 2 of its 6 changed files, and CI went
   green on the wrong code. Run `python3 "$ONEPEEK_WORKSPACE/.github/workspace/done_check.py" <worktree> --pr <repo>#<n> --expect <files>` before you report
   (add `--since <sha>` when your commits join a longer pull request).
   It must end `DONE CHECK OK`.
4. **Run local CI on your worktree path**, not on the repo name. `local_ci.py <repo>` reads the shared checkout. For a
   `.github` change, set `ONEPEEK_WORKSPACE` to a folder whose `.github` links to your worktree, so the shared lint is yours.

## Match the real environment
5. **CI runs Linux, not macOS.** macOS has bash 3.2, CI has bash 5, and pipes behave differently. Run script tests in a
   Linux container. `local_ci.py` does this from L2 on; until then, mount the worktree and run the changed tests:
   `docker run --rm -v "<worktree>:/w" -w /w python:3.14-slim sh -c 'apt-get update -qq && apt-get install -qq -y git >/dev/null && pip install -q <the test dependencies CI installs> && python3 -m unittest <test files>'`
   (for `.github`, `pyyaml==6.0.3`, as `workflow-lint.yml` installs it).
6. **Use the tool versions CI really uses.** Read them from the one constant that CI uses, never from your laptop. On
   2026-11-06 a local CI container ran Python 3.12 while CI ran 3.14.
7. **Verify platform behaviour in the official docs before you rely on it** (GitHub contexts, AWS IAM, Terraform, ECS).
   Cite the page in the pull request. Two examples: `job.workflow_sha` exists but actionlint 1.7.12 does not know it, and a
   tag pushed with `GITHUB_TOKEN` starts no workflow.
8. **Dependabot runs get no Actions secrets.** A check that needs an App token fails on every Dependabot pull request
   unless the secret is also a Dependabot secret.

## Tests that can fail
9. **Every new test must fail when the code it guards is broken.** Break the code once per behaviour (a mutation), see the
   test fail, restore it, and list the mutations in your report. A test that cannot fail is worse than none.
10. **Mocks must give distinct values.** A Terraform mock that gives every role the same ARN made a set comparison
    vacuous (infra #174). Build values from names, or assert lengths and members, not only sets.
11. **Command stand-ins must behave like the real command.** A fake `docker login --password-stdin` or `cosign` must read
    its stdin. Otherwise the writer gets SIGPIPE and exits 120 or 141 on Linux only (.github #54, #55).
12. **Test the hostile and odd inputs, not only the happy path.** Cover a flag with a value before the subcommand
    (`gh -R x run cancel`), shell operators (`&`, `;`, `(`, backticks, `$( )`), quotes, a newline in a name, an empty or
    unreadable file, a tie between equal timestamps, a deleted branch, and a fork.

## Code
13. **Never detect an error by searching its message for text.** `"404" in str(error)` matched a branch named
    `fix/404-page`. Use the status code or the exit code.
14. **Safety checks fail closed.** A read error, unparsable input or a missing value means refuse. It never means pass.
15. **Parse structured data; never substring-match it.** JSON settings are parsed and their keys checked. A shell
    comment is found by a shell-aware scanner, not by `shlex` comment mode.
16. **One copy of a rule.** Logic copied into several files drifts. If the code needs the same logic twice, ask for a
    shared script in the design rather than copying it. The release-lock block lives in 10 files today, which is a known
    debt.
17. **Never invent.** Do not add a field, limit, name, permission, default or behaviour the packet does not state. Stop
    and report the gap. An invented value that looks reasonable is still a defect.
18. **Least privilege, exactly as the docs list it.** Never widen an IAM action, resource or GitHub permission "to make it
    work". IAM refuses a trust policy that names a role that does not exist.

## Before you report
19. **Self-review against the packet**, line by line, then against this file. Report what you checked.
20. **The report proves it:**
    - files per commit;
    - the last line of every gate;
    - the mutations you ran;
    - `DONE CHECK OK`;
    - the gaps you found.

    No narrative.

## Never wait silently
21. **Do not wait on CI, a review or a dependency.** On 2026-11-06 an agent sat about 70 minutes waiting on its own
    background work. Write the status file, push if you have push authority and local CI is green (otherwise keep the worktree path), and
    hand back `BLOCKED` within about 5 minutes; the coordinator
    watches and resumes you (`agent-operations.md` §5.5).

## Added 2026-11-06 (watch until holding, agent-operations.md §8a)
22. **`.context/` is never committed.** A builder committed its status file. It is in `.gitignore` now, and `done_check.py` fails on it.
23. **Prove platform behaviour before a design depends on it.** `journalctl --follow` on systemd 252 writes no cursor when stopped; only a container test showed it. Test it in a container or the sandbox, and cite the result.
24. **A fresh worktree has no dependencies.** Install them (`pnpm install`, `uv sync`) before committing, so the pre-commit hooks run. Never skip a hook. If a repo's hook needs a tool the repo never installs, that is a bug to report.
25. **Decision numbers:** check the open docs pull requests, not only `main`, before taking the next number.
26. **GitHub's API can show the old head for a few seconds after a push.** `done_check.py` re-reads briefly; do not "fix" a failure by pushing again.
27. **Tests that run `git` clear Git's repository-local variables first** (`git rev-parse --local-env-vars`: `GIT_DIR`, `GIT_INDEX_FILE` and the rest). A hook passes them on, and an inherited `GIT_DIR` made a fixture rewrite another repo's config (.github #60).
28. **The local CI container must hold every tool CI's steps call.** The Python 3.14 container (#57) lacked `uv`, so `uvx` steps failed locally but not on CI (infra #177). `uv` is baked in at `local_ci.UV_VERSION`, and a test pins it. A new tool in a workflow step is added to the Dockerfile in the same pull request.
29. **Workspace paths in the kit start from `"$ONEPEEK_WORKSPACE"`, quoted.** The shared agents and skills said `../.github/workspace/packet.py` and `.github/workspace/done_check.py`, which lead nowhere from a worktree under the scratchpad (CodeRabbit on 7 rollout pull requests, 2026-11-06). Never write a relative path to another repo.
