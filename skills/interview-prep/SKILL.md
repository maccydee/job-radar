---
name: interview-prep
description: Prepare the user for a job interview from what they actually sent. Use when the user has an interview booked or asks to prepare for one, practise, run a mock interview, or research a company or interviewer before a call. Starts from the job-radar interview pack, then researches the company and the named interviewers, maps likely questions to real examples from the master CV, and gives honest answers for the thin areas. Never invents experience.
---

# interview-prep

The expensive mistake in interview prep is preparing from the CV you remember
rather than the one the employer read. This skill starts from the record, then
adds the half that needs the web and judgement.

## 1. Build the pack first

```bash
job-radar interview <company|uid|url> --stage "recruiter screen"   # or "hiring manager", "technical"
```

It prints the path of a markdown file. Read all of it before doing anything
else. It is built with no model and no web access, from the application record:
the CV that was sent, the figure given on the form, the dates, the places the
CV does not answer the posting, and any salary evidence on file.

Two things in it deserve a stop:

- If it says `no CV is recorded for this application`, ask the user which file
  they sent. Do not assume the master CV or the newest draft.
- If a section says `UNMEASURED`, that check could not run (no posting text, or
  no CV text). It is not a clean bill of health. Say so and fill the gap with
  the user instead of skipping it.

## 2. Research, with a verify-before-use rule

Research the company and each named interviewer on the open web. The rule for
everything found: **do not state a fact about a person or a company that is not
on a page you have just read.** Open the page, read the sentence, and quote or
paraphrase that sentence. Do not rely on memory of what a person "usually" does,
on a name that looks familiar, or on a search snippet.

For each interviewer, find what they have said or built in public (their role,
a talk, a post, their team's open roles). If you cannot find a page that says
it, write `not found`, not a guess. A confident wrong detail about an
interviewer is worse than saying nothing, because the user will repeat it to
their face.

For the company, find: what it sells and to whom, how it makes money, recent
news (dated), the size and shape of the engineering or security organisation,
and any public pay evidence. File pay figures you find with
`job-radar evidence add "<company>" --source <where> --url <page> --figures "<what it says>"`
so the next session does not redo the search.

## 3. Map the questions to real examples

Take the likely questions for the stage (the pack lists them) and match each to
an example from the **master CV only**. If the master CV does not contain an
example, say there is none and ask the user whether one exists. Never write an
answer that depends on experience, a number or an outcome that is not in the
master CV or that the user has not told you in this conversation. The claims
file (`claims.local.yaml`) lists claims the user has ruled out; check an answer
against it with `job-radar cvcheck`, which also takes a text file.

## 4. The thin areas

The pack lists posting requirements the CV does not mention. For each one:

- If the user has the experience but the CV does not say so, help them put it
  in their own words.
- If they do not, write an honest bridge: what they have done that is nearest,
  said plainly as nearest, and how they would close the gap. No claim of the
  missing skill.

## 5. Offer a mock interview

Offer to run one: ask the questions one at a time, let the user answer, then
give feedback on the answer against the CV and the posting. Do not coach a
claim the user cannot defend under a follow-up question.

## Rules

- Everything the user says about themselves is theirs to correct. When a draft
  answer states a figure, show where it came from.
- Do not write to the application record unless asked. Salary figures go in
  `job-radar evidence add` with a source; nothing else here changes the
  database.
