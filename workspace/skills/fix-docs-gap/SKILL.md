---
name: fix-docs-gap
description: When code needs something the docs (LLD, HLD, PRD, TRD, standards) do not say, or two docs disagree, fix the docs first with a pull request in onepeek-docs, then continue the code. Use whenever a task packet is silent, ambiguous or contradictory, instead of guessing in code.
---

# Fix a docs gap before the code

Code never merges ahead of its docs (`onepeek-docs/standards/spec-to-code.md` §1 step 3). A guess in code is drift, even when the guess is good. A gap found while building sends the unit back to step 2 of `standards/development-cycle.md` (design ready) until the fix is committed on the change set's docs pull request; it is not a reason to start another stage meanwhile.

## Steps
1. **Stop the code change.** Write down the gap in one sentence: what the code needs, which packet section is silent (or which two sections disagree), and the exact lines.
2. **Find the owner:** the lowest document that should say it.
   - A module's or client's behaviour goes in its LLD.
   - A platform type or rule goes in the platform LLD that owns it (`tools/architecture/platform_eval.py` checks there is one owner per type).
   - A cross-cutting rule goes in the HLD or a standard. A change to the HLD needs a founder decision.
3. **Propose the smallest fix and its reason,** in plain words.
   - If it is a product or architecture choice, ask the founder and record the answer in `decisions/`.
   - Otherwise use the recommended default and mark it provisional in the doc.
4. **Commit the fix on the change set's one docs pull request** in `../onepeek-docs` (branch `docs/<unit>`; open it only if the unit has none yet, never a second one). Run every gate listed in that repo's `AGENTS.md`, including `python3 tools/context.py --check-all`, and push (`standards/development-cycle.md` §2a).
5. **Then, without waiting for a merge:**
   - move this repo's `docs-version.txt` to that new docs commit;
   - regenerate the packet;
   - continue the code from the new packet.
   The docs pull request merges first, with a merge commit, when the whole change set ships.

## Never
- Put the missing rule in a code comment or a test instead of the docs.
- Edit a founder decision or an accepted ADR. A new decision supersedes it.
- Commit, push or merge without an explicit instruction from a person.
