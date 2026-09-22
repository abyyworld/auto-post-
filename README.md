# auto-post

Build in public without writing every post from scratch. Three times a week this looks at what
changed in my public GitHub repositories, drafts X posts about anything worth sharing, and
opens them as a GitHub issue. I read them on my phone, tap copy, and post the ones I like.
LinkedIn gets a draft only for real milestones.

Nothing posts on its own.

## How a run works

```
scan.py   what is new since the last issue          code, with a self test
draft.py  is any of it worth a post, and the text   a model, following rules.md
scan.py   measure every X post against 280          code
GitHub    open the issue, assign it to me           workflow, Mon Wed Fri 07:30 UTC
```

The split is deliberate. Deciding which commits are new, and counting characters, are what a
model gets quietly wrong, so they are done in code. The model only does the judgement and the
writing. That also makes it portable: `rules.md` names no vendor, and `draft.py` is the only
file that would change to use a different model.

"New" means new since the previous drafts issue. Each issue carries a hidden marker with every
repository's head commit, so work on a branch that is merged days later still counts, and
nothing is drafted twice. There is no state file and no bot commits; the issues are the log.

## Setup

1. Add a repository secret `ANTHROPIC_API_KEY` under Settings, Secrets and variables, Actions.
   Without it the issue still arrives, listing what changed without drafts.
2. Run it once by hand from the Actions tab (Post drafts, Run workflow) to see a batch.

## What each draft gives you

- an X post, or a short thread, ready to paste; it also fits Bluesky, Threads and Mastodon
- the reply carrying the link, since X shows posts with an outside link to fewer people
- what image or clip to attach
- a LinkedIn version when it is a milestone
- every number in the post, with where it came from, to check before posting

## Changing things

| To change | Edit |
| --- | --- |
| handles, links, facts that must match, limits, which repos count | `config.json` |
| what is worth a post, the voice, the format | `rules.md` |
| the schedule | `.github/workflows/post-drafts.yml` |
| the model or provider | `draft.py` |

Assistants start at [`AGENTS.md`](AGENTS.md).
