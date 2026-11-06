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
   green on the wrong code. Run `python3 .github/workspace/done_check.py <worktree> --pr <repo>#<n>` before you report.
   It must end `DONE CHECK OK`.
4. **Run local CI on your worktree path**, not on the repo name. `local_ci.py <repo>` reads the shared checkout. For a
   `.github` change, set `ONEPEEK_WORKSPACE` to a folder whose `.github` links to your worktree, so the shared lint is yours.

## Match the real environment
5. **CI runs Linux, not macOS.** macOS has bash 3.2, CI has bash 5, and pipes behave differently. Run script tests in a
   Linux container. `local_ci.py` does this from L2 on; until then, use `docker run python:3.14-slim`.
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
