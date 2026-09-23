# auto-post

Build in public without writing every post from scratch. Three times a week this looks at what
changed in my public GitHub repositories, drafts posts about anything worth sharing, and opens
them as a GitHub issue. I read them on my phone, tap copy, and post the ones I like.

| Platform | How often | What goes there |
| --- | --- | --- |
| X | every item, about 3 a week | results, releases, build-in-public |
| Reddit | once or twice a month | a strong result or a release, in the one subreddit it fits |
| Instagram | a few a month | only items that already have a clip, GIF or plot |
| LinkedIn | rarely | milestones: a paper, a release, a launch, a new role |

Nothing posts on its own.

## How a run works

```
scan.py   what is new since the last issue          code, with a self test
draft.py  is any of it worth a post, and the text   a model, following rules.md
scan.py   measure every post against its platform    code
GitHub    open the issue, assign it to me           workflow, Mon Wed Fri 07:30 UTC
```

The split is deliberate. Deciding which commits are new, and counting characters, are what a
model gets quietly wrong, so they are done in code. The model only does the judgement and the
writing. That also makes it portable: `rules.md` names no vendor, and switching model provider
means replacing `draft.py` and changing three lines in the workflow.

"New" means new since the previous drafts issue. Each issue carries a hidden marker with every
repository's head commit, so work on a branch that is merged days later still counts, and
nothing is drafted twice. There is no state file and no bot commits; the issues are the log.

## Setup

1. Add a repository secret `ANTHROPIC_API_KEY` under Settings, Secrets and variables, Actions.
   Without it the issue still arrives with what changed and the digest, ready to hand to any
   assistant with `rules.md` and `config.json`; run `python3 scan.py check` on what it writes.
2. Run it once by hand from the Actions tab (Post drafts, Run workflow) to see a batch.

## What each draft gives you

- an X post, or a short thread, ready to paste, with the reply that carries the link
- what image or clip to attach
- a Reddit title and the facts for the body, with the subreddit and flair, when one fits;
  rewrite the body in your own words, because several subreddits remove AI-written posts
- an Instagram caption when there is already a clip or plot to post
- a LinkedIn version when it is a milestone
- every number in the post, with where it came from, to check before posting

Every post is measured in code against its platform's limits. For X that means counting the way
X does, including that `scan.py` or `README.md` in a post counts as a 23 character link.

## Changing things

| To change | Edit |
| --- | --- |
| handles, links, facts that must match, limits, which repos count | `config.json` |
| what is worth a post, the voice, the format | `rules.md` |
| the schedule | `.github/workflows/post-drafts.yml` |
| the model | `draft.py` |
| the provider | `draft.py`, plus the secret and package in the workflow's "Write the drafts" step |

Assistants start at [`AGENTS.md`](AGENTS.md).
