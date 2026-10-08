# Skills

Claude Code skills that pair with job-radar. They are usable on their own, and
they are usable together: `setup` can read a CV to propose what to search for,
and `screen-role` reads a job description with the same dealbreakers the
scanner uses.

## What lives here, and where it comes from

| Skill | Source of truth | Vendored at |
|---|---|---|
| `rate-cv` | [maccydee/rate-cv](https://github.com/maccydee/rate-cv) | `fc8d1ab` |
| `screen-role` | this repo | native |
| `job-radar-setup` | this repo | native |
| `mail-sync` | this repo | native |
| `interview-prep` | this repo | native |

`rate-cv` ships in two places on purpose. It stands on its own for anyone who
only wants a CV scored, and it ships here so that cloning job-radar gets you a
working set rather than a scanner and a list of things to go and install.

**Its own repository is the source of truth.** The copy in this directory is
generated. Do not edit it here: changes belong upstream, and
`.github/workflows/sync-skills.yml` checks weekly that this copy still matches
and opens a pull request when it does not. Editing the copy directly is how two
versions of the same skill quietly stop agreeing with each other.

## Also required for document generation

The drafting prompts and two of the four quality gates call
[natural-writing](https://github.com/maccydee/natural-writing). It is not
vendored here because it is a general writing skill with a life of its own,
and generation is worse without it. `job-radar generate` names any skill it
could not find and drafts anyway, rather than leaving you to guess:

```bash
git clone https://github.com/maccydee/natural-writing ~/.claude/skills/natural-writing
```

## Installing

Nothing needs copying for job-radar's own use. `generate` reads this directory
straight out of the checkout, resolved from the package rather than from the
working directory, so a fresh clone can screen and draft with no setup step.
It looks in `~/.claude/skills` first and falls back to here, so a skill you
have edited yourself is the one that gets used.

To use them in Claude Code from any folder, they need to be in
`~/.claude/skills`. `job-radar setup` asks whether to copy them there, and
`job-radar install-skills` does it at any time (`--dest` names another folder).

```bash
job-radar install-skills
```

It never overwrites: a skill already there that differs from the one shipped
here is reported and left alone, and a copy that could not be made is reported
and makes the command exit non-zero. Scripted setups install nothing unless
given `setup --defaults --install-skills`.

`natural-writing` (see above) is a separate repository, so it is asked about
separately: setup offers to download it with `git` when it is missing, and
`install-skills --fetch-natural-writing` (or `setup --install-skills
--fetch-natural-writing`) does it without a question. It is the only step that
downloads anything, and a folder already there is never replaced.
