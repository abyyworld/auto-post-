# auto-post

Drafts posts about Akbar Juraev's work from what changes in his public GitHub repositories.
Three times a week it works out what is new, writes X posts, plus a Reddit, Instagram or
LinkedIn version when an item suits one, and opens them as a GitHub issue to review, copy and
post by hand.

This file is the entry point for every assistant, whichever vendor. `CLAUDE.md` only points
here. Read this, then `rules.md`, and you know the whole repository.

## The job

Read `rules.md` and follow it. That is the whole job.

Whatever runs this on a schedule, the prompt is these three lines and nothing else:

```
Read rules.md in this repository and follow it exactly.
Use the digest from `python3 scan.py scan --json` as the list of what changed.
Output only the drafts, in the format the rules specify.
```

**Nothing here ever posts on its own.** It drafts; a person reads, edits and posts. Do not
wire it to a posting API without being asked in so many words.

## Files

| File | What it is | Who edits it |
| --- | --- | --- |
| `rules.md` | the procedure: what is worth a post, the voice, the output format | the owner, rarely |
| `config.json` | who, links, which repositories count, platform limits, facts that must match | the owner, whenever a fact changes |
| `scan.py` | the deterministic half: what is new, the issue body, each platform's length and hashtag checks. Standard library only | only to fix a bug, with a test |
| `x-tlds.txt` | the top-level domains X turns into links, from twitter-text, so X posts are counted as X counts them | only to refresh from a newer twitter-text |
| `draft.py` | the judgement half: sends rules, config and digest to a model, prints drafts | only to change provider or model |
| `.github/workflows/post-drafts.yml` | the schedule: Monday, Wednesday, Friday 07:30 UTC | rarely |

There is no state file. Each drafts issue (label `post-drafts`) carries a hidden
`<!-- auto-post-state {...} -->` marker with the time of its scan and each repository's head
commit. The next scan reads the newest one. The issues are the log.

## Commands

```bash
python3 scan.py since                            # when the current window starts, and why
python3 scan.py scan                             # what changed, readable
python3 scan.py scan --json --out digest.json    # the same, for draft.py or an assistant
python3 scan.py scan --since 2026-09-15T00:00:00Z
python3 draft.py digest.json --dry-run           # the exact prompt, no key needed
python3 draft.py digest.json > raw.md            # needs anthropic and ANTHROPIC_API_KEY
python3 scan.py check raw.md                     # flags posts over their platform's limits, drops em dashes
python3 scan.py compose digest.json --drafts raw.md   # the issue body
python3 scan.py selftest                         # the checks behind all of the above
```

`scan.py` reads `SCAN_TOKEN` or `GITHUB_TOKEN` from the environment and works without either
for a small run.

## Rules for anything editing this repository

1. **Commits are authored and committed as `abyyworld <annolieberto@gmail.com>`.** Set it per
   repository before the first commit:
   `git config user.name abyyworld && git config user.email annolieberto@gmail.com`.
   No other name or email, whatever the machine's global git config says.
2. **No AI co-author trailers and no AI identity as author or committer**, in commits or pull
   requests, so the Contributors list shows only the owner. No `Co-Authored-By`, no session
   links, no "Generated with" lines.
3. **Never decide by hand what is new.** `scan.py` owns the window and the commit list. If its
   answer looks wrong, fix `scan.py` and add a case to `selftest` in the same commit.
4. **Data goes in `config.json`, procedure goes in `rules.md`.** Do not restate a handle, a
   limit or a fact in prose anywhere else.
5. **Never commit drafts, digests or issue bodies.** They live in the issues. `.gitignore`
   covers the working files.
6. **No em dashes**, in posts, docs or commit messages.
7. **Public repositories only**, unless the owner flips `scan.public_only`. Never mention a
   private project in a draft, even one you know about from elsewhere.

## Switching the schedule on

1. Add a repository secret named `ANTHROPIC_API_KEY` under Settings, Secrets and variables,
   Actions. Without it the issue still arrives three times a week, carrying the list of what
   changed; paste that and `rules.md` into any assistant to get the drafts.
2. GitHub only runs scheduled workflows from the **default branch**. Keep the workflow there.
   `workflow_dispatch` in the Actions tab runs it by hand at any time.

Issues are assigned to the owner, so every batch arrives as a GitHub notification: email, and a
push on the phone with the GitHub app.

## Running it by hand in a chat assistant

With no API key and no workflow, an assistant that can run Python does the same job:

1. `python3 scan.py scan --json --out digest.json`
2. Follow `rules.md` against that digest and write the drafts.
3. Optionally open the issue from `python3 scan.py compose digest.json --drafts <drafts file>`
   with the label in `config.json`, so the next run starts where this one stopped. Skip this and
   the next run simply covers the same window again.

## Moving this to a different assistant

Nothing here depends on one vendor. The rules are Markdown, the config is JSON, the scanner is
standard library Python. Only `draft.py` calls a model, through the Anthropic SDK. To switch
provider, replace that one file; its contract is to read `rules.md`, `config.json` and the
digest and print the drafts on stdout.

| Option | Where it runs | Portability |
| --- | --- | --- |
| GitHub Actions, the workflow in this repo | GitHub | highest, survives any vendor change |
| Claude Code routines | Anthropic cloud | reads this repo directly |
| OpenAI Codex, pointed at this repo | OpenAI cloud | reads `AGENTS.md`, which is why this file exists |
| Any cron box calling any model API | yours | highest, and the most work |

When switching, run `python3 draft.py digest.json --dry-run` and give the new setup the same
prompt. The drafts should keep the section 7 format exactly, or `scan.py check` cannot measure
them.
