## A-001 | gap-fill | OPEN
The request names `applications.local.yaml` as an import source. `store.migrate` already imported it into `role_state` on 18 Aug 2026, so the importer reads `role_state` rather than the YAML a second time.
Alternatives: parse the YAML again (double-counts and trusts a file that is stale since September); read both and reconcile.

## A-002 | gap-fill | OPEN
`role_state.updated_at` is when a status last changed, not when the application went in. Where a note carries no date, the import uses `updated_at` and marks the row ESTIMATED (`source = import:updated_at`) rather than leaving the date blank or inventing a better one.
Alternatives: skip undated roles (loses most of the 25 open applications); ask for each date (blocks the work).

## A-003 | gap-fill | OPEN
The mailbox read is a Claude skill driving the Outlook MCP, not a scan step. The MCP's Microsoft Graph login lives in the desktop session; the headless CLI and the GitHub Actions scan have no such login. The CLI half (classify, match, propose, apply) is pure and testable without it.
Alternatives: add an IMAP reader to the CLI (needs the mailbox password stored on disk, which this project deliberately avoids); register a Graph app (account creation, out of scope).

## A-004 | gap-fill | OPEN
A partial mailbox read exits non-zero (3) unless `--allow-partial`. The repository's own rule is that a failure must not render like a success, and a read that skipped Deleted Items is exactly how four rejections went unseen this week.
Alternatives: warn and exit 0 (the warning scrolls away); refuse to write any proposal (loses real signal from the folders that were read).

## A-005 | gap-fill | OPEN
`interview` builds a deterministic pack and does not call a model. Research on the company and interviewers needs the web and judgement, so that half is a skill file Claude follows, with the verify-before-use rule. Keeps the command free to run, in line with "nothing generates unless you asked".
Alternatives: a `claude -p` generation like `generate` (spends tokens on every run, and a stale pack looks as good as a fresh one).

## A-006 | gap-fill | OPEN
`cvcheck` and the follow-up drafts are written for Alex's rules (no em-dash, banned claims) through `claims.local.yaml` and a `--name` flag rather than hardcoded, because the repository is public and other people use it. His real claims file is local and gitignored.
Alternatives: hardcode his rules (leaks personal claims into a public repo).
