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
30. **"No unresolved threads" is not "reviewed".** The first watcher would have merged while CodeRabbit was still running and before any independent review, because zero threads looked like a pass (F36). A merge gate checks the review gates themselves: the bot's check finished on the head, and a recorded verdict at that exact head.
31. **A required input of a reusable workflow is checked only when the call runs.** Since `.github` v1.16.0 `release-lock` required `scope`, and the infra release workflows never passed it; release paths run rarely, so nobody saw it (F35). Adding a `required: true` input means updating every caller in the same change set; `check_caller_inputs.py` in `pins.yml` and `local_ci.py` now fails a call without one.
32. **A push is proven by the remote, never by its output.** After every push, compare `git ls-remote origin refs/heads/<branch>` with `git rev-parse HEAD`, or run `done_check.py`. The coordinator filtered push output for "error|rejected", and a detached HEAD's `fatal:` slipped through, so a review ran on the old head (.github #64, 2026-11-06).
33. **A test never counts loop turns inside a wall-clock limit.** A lock test counted waits inside a 3-second cap; on a loaded runner one turn took the whole cap, so CI failed now and then with an IndexError (T-37). End the loop on a counted limit, or assert the time spent.
34. **Undo a mutation with the reverse edit, never `git checkout <file>`.** `git checkout` throws away every uncommitted change in the file, the real fix included. The coordinator lost its own fix this way twice in one day (.github #64 `done_check.py`, infra #177 `stuck-deploys.tf`). Commit the fix first, or mutate a copy.
35. **Stop only your own containers:** start them with `--name <you>-<purpose>` or a `--label`, and kill by that name or label, never by image. A builder killed every `python:3.14-slim` container while stopping its own load test, which could have stopped other agents' runs (T-37, 2026-11-06).
36. **A plan before an apply cannot prove reads of resources the apply creates.** The warm pool added EventBridge Scheduler resources the CI plan role could not read; the laptop apply (admin rights) passed, and the next CI plan failed, so infra #181 could not merge (F37, infra #183, 2026-11-06). When a change adds a new resource type, grant the CI plan role its reads in the same change, and re-run the CI plan straight after any laptop apply.
37. **Agent shells do not inherit `ONEPEEK_WORKSPACE`, and shell state does not persist between commands.** Three agents stopped at step 0 because the variable was unset (F38, 2026-11-06). Every brief says to prefix each command that needs it with `export ONEPEEK_WORKSPACE=<the folder holding the repos> &&`; an agent that finds it unset hands back BLOCKED rather than guessing a path.
38. **A unit test never downloads from the network.** A local CI test fetched tflint's AWS plugin from the GitHub API and failed CI with a 504 (T-54, .github #72). Pre-seed what the test needs (a local plugin, a fixture) and run the test with `--network none` where it can.
39. **Undo a mutation by restoring the exact span at its own position, never by a global replace of the new text.** A short replacement string matched elsewhere in the file and quietly changed another line (T-53). Restore from a saved copy of the file, or with the reverse edit anchored on unique context, then check `git diff` equals the diff saved before the mutation (empty when the fix was committed first), and `git status --porcelain` shows no file the mutation left behind. Run the unmutated baseline in the same invocation first: a batch whose every mutant "fails" on an import error proves nothing (a reviewer passed two modules as one zsh argument).
40. **A text pattern that decides a privilege or a gate is tested against hostile inputs taken from the real files.** Three in one evening (2026-11-06): `\baws \b` matched prose ("the stacks/aws suite") in a heredoc, so a step was skipped as needing AWS (T-58); `\buv\b` did not match `uvx`, so a container step lost the network (T-64); a `Lane: hotfix` line inside a code fence, an indented block or `<details>` would have let a feature pull request merge as a hotfix (T-53, caught in review; round 2's fence stripping still failed open on a nested fence, so the rule became "the first line only"). List each tool by its full name, derive test cases from every real command or body form, add the near-misses (longer names, prose, quoted, fenced, indented, heading), and show one mutation per rule.
41. **A gate never prints OK for a step it did not run.** Local CI skipped the infra Terraform tests on a false match and still ended `LOCAL CI OK` (T-58). A skipped decisive step (one CI runs as a gate, such as `terraform test`) ends NOT OK, naming the step; a step that only CI can run (it needs a repository secret) is listed as SKIP and named in the summary.
42. **A rule applied on one code path is not a rule.** The "docker steps stay on the host" check ran only for `--skip-docker`; the container decision never consulted it, so a docker step ran where Docker is absent (T-67). Put the rule in one helper that every path deciding the same thing calls, and test each path.
43. **A gate that stops at the first failure hides the next one.** Four local CI faults surfaced one per fix on the same repo (T-58, T-64, T-67, T-68, 2026-11-06), each a fresh brief and review. When a check fails for a reason outside the slice, run the whole list (`local_ci.py --keep-going` once it exists) and report every failure at once, so they are fixed as one batch.
44. **`LEFTHOOK_EXCLUDE` is skipping a hook.** A kit-sync commit excluded the format hook because the repo could not run it (browser-extension #38, T-33). It counts like `--no-verify`: hand back BLOCKED with the hook's error instead; the broken hook is an unblock task.
45. **A stacked pull request closes itself when its base branch is merged and deleted.** .github #76 and infra #192 were auto-closed when #74 and #190 squash-merged (F39, 2026-11-06) and had to be raised again. Base a dependent pull request on the trunk with a `Depends on:` line, or retarget it to the trunk before its base merges; `done_check.py --since` is for commits added to an existing pull request, never for a replacement one.
46. **Read a machine-readable line from one fixed position, not by parsing markdown.** Stripping fences, `<details>` and comments with regexes kept failing open (a nested fence, a setext heading, a lazy list line). The lane is read from the body's first non-empty line only (T-53 re-scope); do the same for any new marker a tool reads from free text.
47. **A child started with `start_new_session=True` no longer gets the terminal's Ctrl-C.** The merge-run timeout killed the process group only on timeout, so a Ctrl-C ended the watcher and left `ship_change_set.py --go` able to merge (.github #82 review). Kill the group on every exit path (`except BaseException`), not only on timeout.
48. **Check a repo's start-up refusals before passing it an environment variable.** Passing `ONEPEEK_WORKSPACE` to backend steps would trip CFG-R8b, which refuses any unknown `ONEPEEK_*` variable (.github #30, T-87).
49. **Sibling repos come from `$ONEPEEK_WORKSPACE`, else the parent folder.** Lookups written only as "beside this repo" always failed in a scratchpad worktree (backend #122, T-83).
