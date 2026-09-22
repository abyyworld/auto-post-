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

`posts.primary` in `config.json` is where every item goes. Today that is X.

**X, every item.**

- One post within `max_chars`, or a thread of at most `thread_max_posts` when the result needs
  setup. The first post must stand on its own: the result, and why it is surprising.
- When `link_in_reply` is true the link goes in a reply, not the main post, because X shows
  posts carrying an outside link to fewer people. Write that reply too.
- Suggest one thing to attach: a plot or GIF that is already in the repository (the digest's
  README often names one), or what to screenshot or record. Posts with an image or a short clip
  travel much further than text.
- Keep it plain enough to paste unchanged into everything in `also_fits`.

**LinkedIn, milestones only.** A paper submitted or accepted, a release, a new project with a
headline result, a launch, or a role change that `config.json` facts state. At most
`linkedin.max_per_run` per run. Otherwise write `LinkedIn: skip` and nothing else for it.

## 7. Output format

Markdown, because it lands in a GitHub issue. Put every post in its own fenced block so it has
a copy button, and use the block names below exactly: `scan.py check` measures every
`x-post` and `x-reply` block and flags any that are too long. Nothing outside this shape: no
preamble, no closing summary, no offer to help further.

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
