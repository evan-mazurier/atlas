# Ledger outside anchor: setup

What it is: once a day, a small fingerprint (a hash) of the last line of each ledger day file is written to a
separate private GitHub repo. If someone later edits the ledger and re-chains it so the hash chain looks clean, the
fingerprints on GitHub no longer match and the Ledger integrity panel turns red. The ledger content itself never leaves
the PC: only hashes.

What it does and does not promise: it detects edits to closed days of the ledger, including a full re-chain, as long as
the GitHub ruleset stays in place. It raises the bar against accidental or clumsy tampering; it is not proof against an
agent that edits Atlas itself (see Limits).

## Steps for the operator (about 10 minutes)

1. **Create the repo.** GitHub, New repository, owner `example`, name `atlas-anchor`, **Private**, tick "Add a
   README" (so the repo is not empty). Create.
2. **Block rewriting.** In the repo: Settings, Rules, Rulesets, New ruleset, **New branch ruleset**.
   - Ruleset name: `no-rewrite`. Enforcement status: **Active**.
   - Target branches: Add target, **Include default branch**.
   - Rules: tick **Restrict deletions**, **Block force pushes** and **Require linear history**. Leave the rest off (no
     pull request requirement, so the daily push still works).
   - Bypass list: leave **empty**.
   - Create.
3. **Clone it with the SSH URL**, using the SSH key your GitHub account already has on this PC:
   `git clone git@github.com:example/atlas-anchor.git D:\AI\atlas-anchor`. Accept the GitHub host key the first
   time.
4. **Check that a push needs no typing.** The daily task runs with nobody at the keyboard, so the key must not ask for a
   passphrase. In a terminal run `ssh -o BatchMode=yes -T git@github.com`. If it answers "Hi example! You've
   successfully authenticated", it works. If it says "Permission denied" or asks for a passphrase, the key is protected
   by a passphrase: load it into the Windows ssh-agent (`ssh-add`, with the "OpenSSH Authentication Agent" service
   running and set to start automatically), then run the check again.
5. **Tell the orchestrator.** It will do a dry run (`py -3.12 <repo>\tools\anchor_ledger.py --dry-run`), the first
   real run (which also commits a `.gitattributes` file in the clone and records a one-time `anchor_configured` event in
   the ledger), and install the daily task `AtlasLedgerAnchor` (23:55, via `tools\install_anchor_task.ps1`).

## Plain notes

- The pinned address lives in `<repo>\anchor.json` (url, branch, clone folder). The cockpit and the tool both
  read it; they do not trust the local `anchors.jsonl` alone, they compare it with what GitHub holds (fetched every
  10 minutes at most, in the background, into a throwaway copy).
- Until the first successful push, the integrity panel says "Outside anchor: not set up" and everything else works as
  before. After it, a missing clone, file or config turns the status red ("Outside anchor missing"), and a check that is
  switched off from the environment shows amber "check disabled": setting it up is one-way on purpose.
- If GitHub cannot be reached, the panel shows an amber "remote unreachable since ..." line, and red once it has been
  unreachable for more than 2 days. If the newest GitHub commit is more than 2 days old it turns amber, more than 7 days
  red: the daily task has probably stopped.
- If the cockpit ever shows an anchor mismatch, do not "fix" it: it means the ledger or the anchor file differs from
  what was recorded outside. The tool refuses to anchor anything while a mismatch exists.
- A day that is still open is anchored as it stands at 23:55 and again, once final, on the next run. Lines written
  between the last run and a later edit are not covered until the next run.
- The clone folder is protected: only `tools\anchor_ledger.py` may name it from a Claude session, and the Edit and Write
  tools cannot touch it.
- Remove the task with `tools\uninstall_anchor_task.ps1`.

## Limits

This raises the bar against accidental or clumsy tampering. It does not stop everyone. An agent or person that can:

- edit Atlas's own code (the tool, the integrity check, the hook), or the pinned address in `anchor.json`;
- edit `~/.ssh/config` or `known_hosts` (to redirect where the push or the check goes); or
- drive a browser that is logged in to your GitHub account (to remove the ruleset, then rewrite the history);

could defeat it. Keep your GitHub login out of any browser an agent can drive, and treat a silently disabled or
"unreachable" anchor as a reason to look, not as noise.
