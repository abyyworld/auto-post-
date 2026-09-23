# Post drafts: operating rules

Run a few times a week. Read what changed across the public repositories, decide whether any
of it is worth a post, and hand back drafts that can be pasted as they are. This file is the
whole procedure. It is plain Markdown and names no vendor, so any assistant that can read files
and run Python 3 can execute it.

It deliberately contains no handles, limits, facts or repository lists. All of that lives in
`config.json`, so there is exactly one place to change a number and nothing can drift apart.

## 1. What you are given

| Thing | Where | If it is missing |
| --- | --- | --- |
| The rules | this file | stop |
| Who, which repos, platform limits, facts | `config.json` | stop |
| What changed | the digest from `python3 scan.py scan --json` | run it; if it cannot run, say so in one line and stop |

## 2. Do not work out what changed yourself

The digest is the complete list of what is new. `scan.py` decides the window, follows each
repository from the head the last run recorded, and drops merges, bot commits, forks and
private repositories. Deciding which commits are new is the part a model gets quietly wrong, so
it is done in code with a self test behind it.

So: do not browse other repositories, do not add work you remember or can infer, and do not
recount the window. Anything that is not in the digest is not news this run. If the digest looks
wrong, that is a bug in `scan.py` and it is fixed there.

If you cannot run a command, whatever scheduled you may have pasted the digest in. That is the
same thing: use it as given.

## 3. What is worth a post

Roughly strongest first:

1. **A measured result.** A number with a comparison, stated in the README, a commit message or
   release notes. "A 70% accurate labeller built a better map than an 85% accurate one" is the
   shape to look for.
2. **A new public project** with a result or a demo someone can open.
3. **A release**, or a feature a stranger could try from a link.
4. **A milestone** that `config.json` facts already state, when the digest shows the work
   behind it.
5. **Building in public.** A hard bug found and what caused it, a design decision and the reason,
   an approach that failed and what it showed. Only when the commit messages carry the substance;
   never invent the story around a bare subject line.

Not worth a post, however many commits it took:

- dependency bumps, formatting, renames, CI, typos, README wording, number audits, reverts
- your own job search: CVs, cover letters, applications, the opportunity tracker. (Role Radar
  is a product for other people's searches and is fair game.)
- anything the digest does not contain, including private work you happen to know about
- a result already drafted in `previous_drafts`, unless its numbers changed, in which case the
  post says what changed

If nothing qualifies, the first line of the output is exactly `NOTHING TO POST`, followed by
the Skipped section. Silence is fine. Filler costs more followers than it gains.

Return at most `posts.max_per_run` items, strongest first. Several small changes in one
repository can be one post. Do not write a roundup of unrelated small things.

## 4. Facts

- Every number and claim must trace to the digest: a README excerpt, a commit subject or body,
  or release notes. List each one under "Check before posting" with where it came from.
- `config.json` facts outrank everything, including a README. Never contradict them.
- Quote numbers exactly. Keep the unit, the sample size and the conditions when there is room.
- Never claim users, stars, adoption, speed-ups or impact the digest does not show. Never call
  anything first, novel, state of the art or best in class.
- If the README and a newer commit disagree, use the commit and say so under "Check before
  posting".

## 5. Voice

Taken from how the README, the site and the LinkedIn profile are already written.

- Lead with the result, not the effort. "Seed variance outweighed a 32x increase in training
  data" beats "I have been running a lot of experiments".
- One idea per post. Short sentences, first person, plain words.
- Say how it was measured when there is room: n, conditions, hardware.
- End on what it means for someone building similar systems, in one sentence, or on the open
  question the work leaves.
- A question is fine when it is the actual research question. Engagement bait is not:
  "Thoughts?", "Agree?", "Drop a comment", "Like if".
- No em dashes. No emoji. None of: excited, thrilled, proud to announce, humbled, game changer,
  revolutionary, cutting edge, delve, leverage, unlock, journey, 10x, "here's the thing",
  "let that sink in", "big news".
- Hashtags only within `hashtags_max` for the platform, specific ones only, at the end.

## 6. Platforms

Four platforms, each with its own job. What each one is for, its limits and its media advice
live under `posts.platforms` in `config.json`; read the `use` line of each before deciding.

**X, every item.** The regular one.

- One post within `max_chars`, or a thread of at most `thread_max_posts` when the result needs
  setup. The first post must stand on its own: the result, and why it is surprising.
- When `link_in_reply` is true the main post carries the result and the media, and the link goes
  in a reply. X says links are no longer held back, but a post that leads with a link still gets
  far less engagement than one that leads with the result. Write that reply too.
- File names count as links on X: `scan.py` or `README.md` costs 23 characters. Name a file only
  when it matters; `scan.py check` counts it the way X does.
- Suggest one thing to attach, following `x.media`: a plot or a clip that is already in the
  repository, or what to screenshot or record.

**Reddit, sometimes.** Only for a measured result, a release or a launch, and only when an entry
in `reddit.subreddits` fits it. At most `reddit.max_per_run`. Never a subreddit in
`reddit.never`.

- Name the subreddit and its flair, and follow that entry's `format`. If its `verified` is
  false, say so in one line: the rules page has to be read before posting there.
- A text post, never a bare link. Title: the finding with its number and conditions, in plain
  words. Body, in this order: one line saying it is your project, the result in two sentences,
  the setup (hardware, software versions, n, seeds), the method, the numbers, what surprised you
  or what you would do differently, then the link as the last line.
- Several of these subreddits remove posts that read as written by a model, and r/opensource
  bans them outright. Write the body as short, plain, factual lines for the owner to put in his
  own words, and put `Rewrite this in your own words before posting.` above it.
- Nothing that asks for upvotes, anywhere. That is vote manipulation on Reddit.
- Otherwise `Reddit: skip`.

**Instagram, sometimes.** Only when the item already has a video, a GIF or a plot, per
`instagram.use`. At most `instagram.max_per_run`.

- Say which file to use and whether it is a Reel or a carousel, following `instagram.media`.
- The first `first_line_chars` characters show before "more", so the result goes there.
- Caption links do not click: write `link in bio` once and the repository as plain text.
- Hashtags within `hashtags_max`, specific ones, at the end.
- Otherwise `Instagram: skip`.

**LinkedIn, milestones only.** A paper submitted or accepted, a release, a new project with a
headline result, a launch, or a role change that `config.json` facts state. At most
`linkedin.max_per_run`. The first `first_line_chars` characters carry the result. Handle the
link as `linkedin.links` says. Otherwise `LinkedIn: skip`.

**Asking for support.** Only for a launch, and only as a concrete ask that fits the product:
"try it and tell me which boards are missing", not "please like and share". Never ask for votes
on Reddit or for likes anywhere.

## 7. Output format

Markdown, because it lands in a GitHub issue. Put every post in its own fenced block so it has
a copy button, and use the block names below exactly: `scan.py check` measures each block
against its platform's limits in `config.json` and flags any that break them. Nothing outside
this shape: no preamble, no closing summary, no offer to help further.

````
## 1. <a few words naming the result>

**Repo:** [<name>](<url>)
**Why now:** <one line: what in this window makes it worth posting>

**X**
```x-post
<the post, or the first post of a thread>
```
```x-post
<second post of the thread, only if there is one>
```
```x-reply
<reply carrying the link>
```
**Attach:** <the file in the repository, or what to capture>

**Reddit:** <r/subreddit>, flair <flair>
```reddit-title
<title>
```
Rewrite this in your own words before posting.
```reddit-body
<the body, as short factual lines>
```
(or the single line `Reddit: skip`)

**Instagram:** <Reel or carousel>, using <file>
```instagram
<caption>
```
(or the single line `Instagram: skip`)

**LinkedIn**
```linkedin
<the post>
```
(or the single line `LinkedIn: skip`)

**Check before posting**
- <each number or claim>: <where it came from>

## 2. ...

## Skipped
- <repo>: <one line on why it did not make a post>
````

## 8. Changing the rules

Handles, links, limits, platforms, facts and repository filters change in `config.json`. The
procedure changes here. Changing behaviour in either place should not require touching
`scan.py`; if it does, add a case to its `selftest` in the same commit.

## 9. Change log

- 2026-09-22: created. X first, LinkedIn for milestones only, links in the reply.
- 2026-09-23: Reddit and Instagram added as occasional platforms, with a checked list of
  subreddits. X link advice reworded after X said links are no longer held back. Bluesky,
  Threads and Mastodon dropped: not used.
