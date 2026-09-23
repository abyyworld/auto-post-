#!/usr/bin/env python3
"""
Post scanner: the deterministic half.

Works out what changed across your public GitHub repositories since the last batch of
post drafts, and hands that over as a digest. Everything a model gets quietly wrong
(which window to look at, which commits are new, which are bots or merges, which
repositories are private) happens here, in code, with a self test. The model's only
job is judgement: whether any of it is worth a post, and how to say it.

Standard library only, so any machine with Python 3 can run it.

    python3 scan.py since                  when the current window starts, and why
    python3 scan.py scan                   human readable digest of what changed
    python3 scan.py scan --json --out digest.json
    python3 scan.py scan --since 2026-09-15T00:00:00Z
    python3 scan.py render digest.json     print a saved digest as Markdown
    python3 scan.py compose digest.json --drafts drafts.md
                                           the issue body: state marker, drafts, digest
    python3 scan.py check drafts.md        measure every post against its platform's limits
    python3 scan.py selftest               run the built in tests

How "new" is decided. Every drafts issue carries a hidden marker with the time of its
scan and the head commit of each repository's default branch. The next scan compares
each repository from that head to the current one, so a branch merged days after its
commits were written still shows up. A history rewrite is seen through: copies of commits
already reported are recognised by author date and tree. With no recorded head (the first
run) a repository created since scan.start_from is read whole, and any other gives the
commits authored inside the window, since when a commit was pushed is not visible then.
The window never starts before scan.start_from in config.json.

No state is committed anywhere: the issues are the log.

Authentication comes from the environment. SCAN_TOKEN is used when set, otherwise
GITHUB_TOKEN, otherwise no token at all (60 requests an hour, enough for a small run).
"""

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
API = "https://api.github.com"
TRUNCATED = "\n\n[truncated]"
MARKER = re.compile(r"<!-- auto-post-state (\{.*?\}) -->", re.DOTALL)
ISSUE_LIMIT = 60000  # GitHub refuses issue bodies over 65536 characters


# ----------------------------------------------------------------- small helpers

def parse_time(value):
    """GitHub timestamps end in Z, which fromisoformat only accepts from Python 3.11."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_time(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip(text, limit):
    """Cut text to limit characters and say so, rather than dropping the end silently."""
    text = (text or "").strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip() + TRUNCATED
    return text


def inline(text, limit, markdown=False):
    """One line of text, at most limit characters, ending in "..." when shortened.

    With markdown, it is also safe inside a Markdown list item or link text: nothing in it
    can open a tag, a code span or a code fence, or close a link.
    """
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    if markdown:
        text = text.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for char in "`~[]":
            text = text.replace(char, "\\" + char)
    return text


def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def load_config(path=DEFAULT_CONFIG):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ----------------------------------------------------------------- GitHub access

class GitHub:
    """The handful of GET requests the scan needs, and nothing else."""

    def __init__(self, token=None):
        self.token = token

    def _request(self, path, params=None, accept="application/vnd.github+json"):
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": accept,
            "User-Agent": "auto-post-scanner",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8"), response.headers.get("Link", "")

    def get(self, path, params=None, missing=None):
        """One page of JSON. A 404, or a 409 from an empty repository, returns missing."""
        try:
            body, _ = self._request(path, params)
        except urllib.error.HTTPError as error:
            if error.code in (404, 409):
                return missing
            raise
        return json.loads(body)

    def get_all(self, path, params=None, max_pages=None):
        """Every page, following the Link header, or the first max_pages of them."""
        items, url, query, pages = [], path, dict(params or {}, per_page=100), 0
        while url and (max_pages is None or pages < max_pages):
            body, link = self._request(url, query)
            items.extend(json.loads(body))
            url, query, pages = next_link(link), None, pages + 1
        return items

    def readme(self, owner, repo):
        try:
            body, _ = self._request("/repos/%s/%s/readme" % (owner, repo),
                                    accept="application/vnd.github.raw+json")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return ""
            raise
        return body


def next_link(header):
    for part in (header or "").split(","):
        section = part.split(";")
        if len(section) > 1 and section[1].strip() == 'rel="next"':
            return section[0].strip()[1:-1]
    return None


def token_from_env():
    return os.environ.get("SCAN_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


# ----------------------------------------------------------------- previous runs

def draft_issues(issues):
    """Issues only: the issues endpoint also returns pull requests."""
    return [issue for issue in issues or [] if "pull_request" not in issue]


def read_marker(body):
    """The state a previous run left in its issue, or {} if it is missing or mangled."""
    found = MARKER.search(body or "")
    if not found:
        return {}
    try:
        state = json.loads(found.group(1))
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def write_marker(state):
    return "<!-- auto-post-state %s -->" % json.dumps(state, sort_keys=True, separators=(",", ":"))


def resolve_since(config, issues, override=None):
    """Return (moment, reason). The window never starts before scan.start_from.

    The newest drafts issue decides. Its marker records when its scan ran, which is
    a minute or so before the issue was opened, so the marker is preferred and the
    issue's own creation time is the fallback.
    """
    if override:
        return parse_time(override), "given on the command line"

    floor = parse_time(config["scan"]["start_from"])
    issues = draft_issues(issues)
    if issues:
        latest = max(issues, key=lambda issue: parse_time(issue["created_at"]))
        until = parse_time(read_marker(latest.get("body")).get("until") or latest["created_at"])
        if until > floor:
            return until, "last '%s' issue" % config["issue"]["label"]
    return floor, "scan.start_from in config.json"


def issue_until(issue):
    """When the scan behind an issue ran: its marker's time, else when the issue was opened."""
    return parse_time(read_marker(issue.get("body")).get("until") or issue["created_at"])


def previous_heads(issues, before=None):
    """The newest recorded head of each repository, by name.

    Issues are read newest first and the first head seen for a repository wins, so an
    issue whose marker was edited away, or which left a repository out, costs nothing.
    With before, only issues whose scan ran at or before that moment count, so a scan
    started from an earlier point compares from the heads that were current then.
    """
    issues = draft_issues(issues)
    if before is not None:
        issues = [issue for issue in issues if issue_until(issue) <= before]
    heads = {}
    for issue in sorted(issues, key=lambda issue: parse_time(issue["created_at"]), reverse=True):
        for name, sha in (read_marker(issue.get("body")).get("heads") or {}).items():
            if isinstance(sha, str):
                heads.setdefault(name, sha)
    return heads


ISSUE_PAGE = 30  # one request; enough to find the issue a --since run needs


def fetch_draft_issues(github, config):
    """The newest drafts issues, newest first, pull requests left out."""
    return draft_issues(github.get("/repos/%s/issues" % config["issue"]["repo"], {
        "labels": config["issue"]["label"],
        "state": "all",
        "sort": "created",
        "direction": "desc",
        "per_page": ISSUE_PAGE,
    }, missing=[]))


# ----------------------------------------------------------------- selection

def eligible_repos(repos, config):
    """Every repository the scan may talk about. Returns (eligible, skipped counts)."""
    scan = config["scan"]
    excluded = {name.lower() for name in scan["exclude_repos"]}
    owner = config["person"]["github"].lower()
    skipped = {"private": 0, "fork": 0, "archived": 0, "excluded": 0}
    eligible = []

    for repo in repos:
        if (repo.get("owner") or {}).get("login", "").lower() != owner:
            continue
        if scan["public_only"] and repo.get("private"):
            skipped["private"] += 1
        elif repo.get("fork") and not scan["include_forks"]:
            skipped["fork"] += 1
        elif repo.get("archived") and not scan["include_archived"]:
            skipped["archived"] += 1
        elif repo["name"].lower() in excluded:
            skipped["excluded"] += 1
        else:
            eligible.append(repo)

    eligible.sort(key=lambda repo: repo.get("pushed_at") or "", reverse=True)
    return eligible, skipped


def repo_key(repo):
    """Heads are recorded under the repository's id, which a rename does not change."""
    return str(repo.get("id") or repo["name"])


def touched_since(repo, since):
    """pushed_at moves on a push to any branch, so it is a safe first filter."""
    moments = [parse_time(repo.get(key)) for key in ("pushed_at", "created_at")]
    moments = [moment for moment in moments if moment]
    return not moments or max(moments) >= since


def keep_commit(commit, ignore_authors):
    """Drop merges and anything a bot wrote. A commit with no linked account is kept."""
    if len(commit.get("parents") or []) > 1:
        return False
    ignored = {name.lower() for name in ignore_authors}
    login = ((commit.get("author") or {}).get("login") or "").lower()
    name = (((commit.get("commit") or {}).get("author") or {}).get("name") or "").lower()
    if login in ignored or name in ignored:
        return False
    return not (login.endswith("[bot]") or name.endswith("[bot]"))


def summarise_commit(commit):
    message = ((commit.get("commit") or {}).get("message") or "").strip()
    subject, _, body = message.partition("\n")
    return {
        "sha": commit["sha"][:7],
        "date": ((commit.get("commit") or {}).get("author") or {}).get("date"),
        "subject": subject.strip(),
        "body": clip(body, 600),
        "url": commit.get("html_url"),
    }


# ----------------------------------------------------------------- one repository

def current_head(github, repo):
    latest = github.get("/repos/%s/commits" % repo["full_name"],
                        {"sha": repo["default_branch"], "per_page": 1}, missing=[])
    return latest[0]["sha"] if latest else None


PAGE = 100            # commits per request
WHOLE_PAGES = 5       # a new repository's history is read up to 500 commits deep


def commit_key(commit):
    """What a message-only rewrite leaves alone: the author date and the tree.

    Rewording, stripping trailers and remapping author names (filter-repo,
    filter-branch, rebase reword, amend) all keep both, so a rewritten copy of a
    commit already reported is recognised even when its subject changed.
    """
    data = commit.get("commit") or {}
    tree = (data.get("tree") or {}).get("sha")
    return (data.get("author") or {}).get("date"), tree or (data.get("message") or "").strip()


def authored(commit):
    return parse_time(((commit.get("commit") or {}).get("author") or {}).get("date"))


def new_commits(github, repo, since, previous, whole_history=False):
    """Return (commits newest first, head sha, how they were found, count, count_complete).

    With a previous head: what the branch gained since, from the compare when it has a
    common ancestor, else from the branch's own recent history. Either way, commits whose
    identity (commit_key) is already in the previous head's history are copies from a
    rewrite and are left out. With no previous head, a new repository gives its whole
    history and any other gives the commits dated in the window.
    """
    branch, name = repo["default_branch"], repo["full_name"]
    path = "/repos/%s/commits" % name
    if previous:
        compared = github.get("/repos/%s/compare/%s...%s" % (name, previous, branch))
        status = (compared or {}).get("status")
        forward = (compared or {}).get("commits") or []
        if status in ("ahead", "identical", "diverged") and \
                compared.get("total_commits", len(forward)) <= len(forward):
            candidates, complete = forward, True
            head = forward[-1]["sha"] if forward else previous
        else:
            # No common ancestor (a rewritten root commit) or more than one page of change:
            # read the branch itself and let identity sort old from new.
            listing = github.get(path, {"sha": branch, "per_page": PAGE}, missing=[]) or []
            candidates, complete = list(reversed(listing)), len(listing) < PAGE
            head = listing[0]["sha"] if listing else current_head(github, repo)
            status = "unrelated"
        if not candidates:
            return [], head, "since last head", 0, True

        old = github.get(path, {"sha": previous, "per_page": PAGE})
        if old is None and status == "ahead":
            fresh, method = candidates, "since last head"
        elif old is None:
            # The old head can no longer be read, so dates are all there is to go on.
            fresh, method = [c for c in candidates if authored(c) and authored(c) >= since], \
                "since last head, by date"
        else:
            seen, fresh = {commit_key(c) for c in old}, []
            for commit in candidates:
                key = commit_key(commit)
                if key not in seen:
                    seen.add(key)
                    fresh.append(commit)
            method = "since last head" if len(fresh) == len(candidates) and status == "ahead" \
                else "since last head, history rewritten"
        return list(reversed(fresh)), head, method, len(fresh), complete

    if whole_history:
        commits = github.get_all(path, {"sha": branch}, max_pages=WHOLE_PAGES)
        complete, method = len(commits) < PAGE * WHOLE_PAGES, "whole history"
    else:
        listed = github.get(path, {"sha": branch, "since": format_time(since), "per_page": PAGE},
                            missing=[]) or []
        # GitHub's since goes by committer date, which a rebase or rewrite resets; the author
        # date says when the work was done, so older work re-committed later stays out.
        commits = [c for c in listed if authored(c) and authored(c) >= since]
        complete, method = len(listed) < PAGE, "dated in window"
    head = commits[0]["sha"] if commits else current_head(github, repo)
    return commits, head, method, len(commits), complete


def releases_in_window(github, repo, since, now):
    """Published releases with since <= published_at < now, newest first."""
    found = []
    for release in github.get("/repos/%s/releases" % repo["full_name"], {"per_page": 10}, missing=[]) or []:
        published = parse_time(release.get("published_at"))
        if release.get("draft") or not published or not since <= published < now:
            continue
        found.append({
            "tag": release.get("tag_name"),
            "name": release.get("name") or release.get("tag_name"),
            "published_at": release.get("published_at"),
            "body": clip(release.get("body"), 2000),
            "url": release.get("html_url"),
        })
    return found


def repo_changes(github, repo, config, since, now, previous, whole_history=False, releases=None):
    """Return (what happened in the window [since, now) or None, the head to remember)."""
    scan = config["scan"]
    limit = scan["max_commits_per_repo"]
    raw, head, method, total, complete = new_commits(github, repo, since, previous, whole_history)
    kept = [summarise_commit(c) for c in raw if keep_commit(c, scan["ignore_authors"])]
    count = len(kept) + max(total - len(raw), 0)
    if releases is None:
        releases = releases_in_window(github, repo, since, now)

    created = parse_time(repo.get("created_at"))
    is_new = bool(created) and since <= created < now
    if not kept and not releases and not is_new:
        return None, head

    return {
        "name": repo["name"],
        "url": repo.get("html_url"),
        "description": repo.get("description") or "",
        "homepage": repo.get("homepage") or "",
        "language": repo.get("language") or "",
        "topics": repo.get("topics") or [],
        "stars": repo.get("stargazers_count", 0),
        "created_at": repo.get("created_at"),
        "pushed_at": repo.get("pushed_at"),
        "is_new": is_new,
        "found_by": method,
        "commit_count": count,
        "count_complete": complete,
        "commits_capped": count > min(len(kept), limit) or not complete,
        "commits": kept[:limit],
        "releases": releases,
        "readme": clip(github.readme(repo["owner"]["login"], repo["name"]), scan["readme_chars"]),
    }, head


# ----------------------------------------------------------------- the digest

def list_repos(github, config):
    if not config["scan"]["public_only"] and os.environ.get("SCAN_TOKEN"):
        return github.get_all("/user/repos", {"affiliation": "owner", "visibility": "all"})
    return github.get_all("/users/%s/repos" % config["person"]["github"], {"type": "owner"})


def build_digest(github, config, override=None, now=None):
    now = now or datetime.now(timezone.utc)
    scan = config["scan"]
    issues = fetch_draft_issues(github, config)
    since, reason = resolve_since(config, issues, override)
    start = parse_time(scan["start_from"])
    if not override and since == start:
        # The window starts at start_from, after any recorded scan: heads recorded before
        # it would pull in older work, so every repository is read by date instead.
        heads, latest_heads = {}, {}
    else:
        # Compare from the heads current at the start of the window; carry the newest forward.
        heads = previous_heads(issues, before=since if override else None)
        latest_heads = previous_heads(issues)

    eligible, skipped = eligible_repos(list_repos(github, config), config)
    skipped["unchanged"] = 0
    repos, new_heads = [], {}
    for repo in eligible:
        name = repo_key(repo)
        previous = heads.get(name)
        # A repository created since start_from with no recorded head is read whole, so a
        # project pushed with its local history, or first seen while empty, loses nothing.
        whole = not previous and not latest_heads.get(name) and \
            (parse_time(repo.get("created_at")) or start) >= start
        # Publishing a release from a tag already pushed moves no pushed_at, so releases
        # are looked up for every repository.
        releases = releases_in_window(github, repo, since, now)
        if touched_since(repo, since) or releases or whole:
            changes, head = repo_changes(github, repo, config, since, now, previous, whole, releases)
            if changes:
                repos.append(changes)
            else:
                skipped["unchanged"] += 1
        else:
            # Nothing pushed, so the newest recorded head still stands. A repository with
            # none gets one lookup, so the next run can compare against it.
            head = latest_heads.get(name) or current_head(github, repo)
            skipped["unchanged"] += 1
        if head:
            new_heads[name] = head

    return {
        "generated_at": format_time(now),
        "since": format_time(since),
        "since_reason": reason,
        "user": config["person"]["github"],
        "has_changes": bool(repos),
        "repos": repos,
        "skipped": skipped,
        "previous_drafts": [
            {"title": issue.get("title"), "created_at": issue.get("created_at"),
             "body": clip(MARKER.sub("", issue.get("body") or ""), scan["previous_draft_chars"])}
            for issue in issues[:scan["previous_drafts"]]
        ],
        "state": {"until": format_time(now), "heads": new_heads},
    }


def render(digest, max_commits=None, max_repos=None, markdown=False):
    """The digest as text. markdown escapes it for the issue; max_commits and max_repos
    shorten it, saying what was left out."""
    def line(text, limit):
        return inline(text, limit, markdown)

    lines = [
        "CHANGES  %s to %s" % (digest["since"], digest["generated_at"]),
        "Window starts at the %s." % digest["since_reason"],
        "",
    ]
    if not digest["repos"]:
        lines.append("Nothing new in any public repository.")
    shown = digest["repos"] if max_repos is None else digest["repos"][:max_repos]
    for repo in shown:
        tags = ["new repository"] if repo["is_new"] else []
        tags.append(plural(repo["commit_count"], "commit") + ("" if repo.get("count_complete", True) else " or more"))
        if repo["releases"]:
            tags.append(plural(len(repo["releases"]), "release"))
        lines.append("### [%s](%s)  (%s)" % (repo["name"], repo["url"], ", ".join(tags)))
        if repo["description"]:
            lines.append(line(repo["description"], 300))
        for release in repo["releases"]:
            lines.append("- release [%s](%s)" % (line(release["name"], 200), release["url"]))
        commits = repo["commits"] if max_commits is None else repo["commits"][:max_commits]
        for commit in commits:
            lines.append("- `%s` %s" % (commit["sha"], line(commit["subject"], 200)))
        if repo["commit_count"] > len(commits):
            lines.append("- and %d more%s" % (repo["commit_count"] - len(commits),
                                               "" if repo.get("count_complete", True) else " at least"))
        lines.append("")
    if len(shown) < len(digest["repos"]):
        lines.append("And %s more with changes." % plural(len(digest["repos"]) - len(shown), "repository"))
        lines.append("")
    skipped = ", ".join("%d %s" % (value, key) for key, value in digest["skipped"].items() if value)
    if skipped:
        lines.append("Not in this digest: %s." % skipped)
    return "\n".join(lines).rstrip() + "\n"


def renderings(digest):
    """The issue's Markdown list, longest first: all commits, then fewer, then fewer repos."""
    for max_commits in (None, 10, 3, 0):
        yield render(digest, max_commits, markdown=True)
    count = len(digest["repos"])
    while count > 0:
        count //= 2
        yield render(digest, 0, count, markdown=True)


def render_within(digest, limit):
    """The longest of renderings() that fits in limit characters, or None."""
    return next((text for text in renderings(digest) if len(text) <= limit), None)


def for_assistant(digest, limit):
    """The digest as an assistant needs it, as JSON of at most limit characters, or None.

    The state marker is left out. The biggest parts shrink first: README excerpts, then
    commit and release bodies, then previous drafts, then the commit lists themselves,
    and anything cut says so. The result always parses; None means even the smallest
    version does not fit.
    """
    copy = json.loads(json.dumps(digest))
    copy.pop("state", None)
    steps = [
        lambda d: None,
        lambda d: [r.update(readme=clip(r["readme"], 2000)) for r in d["repos"]],
        lambda d: [r.update(readme=clip(r["readme"], 500)) for r in d["repos"]],
        lambda d: [c.update(body=clip(c["body"], 150)) for r in d["repos"] for c in r["commits"]]
                  + [x.update(body=clip(x["body"], 300)) for r in d["repos"] for x in r["releases"]],
        lambda d: d.update(previous_drafts=[]),
        lambda d: [r.update(readme="[left out to fit]") for r in d["repos"]],
        lambda d: [r.update(commits=r["commits"][:10], commits_capped=r["commits_capped"] or len(r["commits"]) > 10)
                   for r in d["repos"]],
        lambda d: [r.update(commits=r["commits"][:3], commits_capped=r["commits_capped"] or len(r["commits"]) > 3)
                   for r in d["repos"]],
        lambda d: [c.update(body="") for r in d["repos"] for c in r["commits"]]
                  + [x.update(body="") for r in d["repos"] for x in r["releases"]],
    ]
    for step in steps:
        step(copy)
        text = json.dumps(copy, indent=1, ensure_ascii=False)
        text = text.replace("<", "\\u003c").replace(">", "\\u003e")  # still the same JSON
        if len(text) <= limit:
            return text
    return None


def compose(digest, drafts=None, limit=ISSUE_LIMIT):
    """The issue body, at most limit characters. The marker goes first and is never cut.

    With drafts: the drafts, then the list of changes in whatever room is left. Without:
    a note, the list of changes, and the digest itself, so any assistant given it with
    rules.md and config.json has every fact a claim must trace to. The list is kept whole
    whenever some version of the digest still fits beside it. Each part is shortened to
    fit rather than cut through, so every block stays closed.
    """
    marker = write_marker(digest["state"])
    if len(marker) + 200 > limit:
        raise ValueError("the state marker alone is %d characters, over the %d limit"
                         % (len(marker), limit))

    def assemble(parts):
        return "\n\n".join(parts) + "\n"

    def listed(text):
        return "<details><summary>What changed</summary>\n\n%s\n</details>" % text.strip()

    def room_after(parts, wrapper):
        return limit - len(assemble(parts)) - 2 - len(wrapper)

    if drafts and drafts.strip():
        head = drafts.strip()
        room = limit - len(marker) - 200
        if len(head) > room:
            keep = max(room - len(TRUNCATED) - 4, 0)  # a negative slice would keep almost everything
            head = (head[:keep].rstrip() + TRUNCATED).strip()
        if sum(1 for line in head.split("\n") if line.startswith("```")) % 2:
            head += "\n```"  # close a fence left open, by the cut or by the model
        parts = [marker, head]
        changes = render_within(digest, room_after(parts, listed("")))
        body = assemble(parts + ([listed(changes)] if changes else []))
        return body if len(body) <= limit else assemble([marker, TRUNCATED.strip()])

    note = ("No drafts this time: no model was available to write them. To get them, give any "
            "assistant rules.md, config.json and the digest below, and ask it to follow rules.md. "
            "Then run `python3 scan.py check` on what it writes before posting, since that "
            "assistant cannot run it.")
    wrapper = "<details><summary>Digest for an assistant</summary>\n\n```json\n%s\n```\n</details>"
    for changes in renderings(digest):
        parts = [marker, note, listed(changes)]
        digest_json = for_assistant(digest, room_after(parts, wrapper % ""))
        if digest_json is not None:
            return assemble(parts + [wrapper % digest_json])

    note += (" The digest was too large to include; run `python3 scan.py scan --json --since %s` "
             "to get it." % digest["since"])
    parts = [marker, note]
    changes = render_within(digest, room_after(parts, listed("")))
    body = assemble(parts + ([listed(changes)] if changes else []))
    if len(body) > limit:  # only the note is left to shorten; the marker is never cut
        body = body[:limit - len(TRUNCATED) - 1].rstrip() + TRUNCATED + "\n"
    return body


# ----------------------------------------------------------------- checking drafts

FLAG = r"> (?:Over the limit: \d+ of \d+ characters\. Trim before posting\.|\d+ hashtags?: the rules allow \d+\.)"
POST_BLOCK = re.compile(r"^```([a-z-]+)[^\n]*\n(.*?)\n```[ \t]*$((?:\n" + FLAG + r"[ \t]*(?=\n|\Z))*)",
                        re.DOTALL | re.MULTILINE)
HASHTAG = re.compile(r"(?<![\w&#/])#(?=\w*[^\W\d])\w+")

NARROW = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
X_LINK_LENGTH = 23


def x_link_patterns(path=HERE / "x-tlds.txt"):
    """X's link rules, ported from twitter-text 3.1.0's extractUrlsWithIndices.

    A link needs no https://. Any domain ending in a real top-level domain becomes one,
    which is why scan.py, README.md and np.dot count 23 on X while config.json does not.
    Trailing punctuation stays outside the link. The names below match the regexes in
    twitter-text/dist/regexp so the two can be compared line by line.
    """
    tlds = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    tld = r"(?:(?:%s)(?=[^0-9a-zA-Z@+-]|$)|xn--[\-0-9a-z]+)" % "|".join(
        sorted(map(re.escape, tlds), key=len, reverse=True))
    latin = "\xC0-\xD6\xD8-\xF6\xF8-\xFFĀ-ɏɓɔɖɗəɛɣ" \
            "ɨɯɲʉʋʻ̀-ͯḀ-ỿ"
    domain_char = "[^/!'#%&()*+,\\\\\\-.:;<=>?@\\[\\]^_{|}~$\\x09-\\x0D\\x20\\x85\\xA0 ᠎" \
                  " -     　￾﻿￿‪-‮" \
                  "؜‎‏⁦-⁩]"
    subdomain = "(?:(?:{c}(?:[_-]|{c})*)?{c}\\.)".format(c=domain_char)
    domain_name = "(?:(?:{c}(?:-|{c})*)?{c}\\.)".format(c=domain_char)
    path_char = "[a-zЀ-ӿ0-9!*';:=+,.$/%#\\[\\]\\-–_~@|&" + latin + "]"
    parens = "\\((?:{p}+|(?:{p}*\\({p}+\\){p}*))\\)".format(p=path_char)
    path_end = "(?:[+\\-a-zЀ-ӿ0-9=_#/" + latin + "]|" + parens + ")"
    url_path = "(?:(?:{p}*(?:{b}{p}*)*{e})|(?:@{p}+/))".format(p=path_char, b=parens, e=path_end)
    preceding = "(?:[^A-Za-z0-9@＠$#＃￾﻿￿]|[‪-‮؜‎‏⁦-⁩]|^)"
    extract = re.compile(
        "(" + preceding + ")"
        "((https?://)?"
        "(" + subdomain + "*" + domain_name + tld + ")"
        "(?::([0-9]+))?"
        "(/" + url_path + "*)?"
        "(\\?[a-z0-9!?*'@();:&=+$/%#\\[\\]\\-_.,~|]*[a-z0-9\\-_&=#/])?)",
        re.IGNORECASE)
    ascii_domain = re.compile("(?:(?:[\\-a-z0-9" + latin + "]+)\\.)+" + tld, re.IGNORECASE)
    return extract, ascii_domain


X_EXTRACT, X_ASCII_DOMAIN = x_link_patterns()


def x_links(text):
    """(start, end) of every span X would turn into a link, in order."""
    spans, position = [], 0
    while True:
        found = X_EXTRACT.search(text, position)
        if not found:
            return spans
        position = found.end()
        before, url, protocol, domain, path = found.group(1, 2, 3, 4, 6)
        start = found.end() - len(url)
        if protocol:
            spans.append((start, found.end()))
            continue
        if before and before in "-_./":
            continue
        last = None
        for ascii in X_ASCII_DOMAIN.finditer(domain):
            last = [start + ascii.start(), start + ascii.end()]
            spans.append(last)
        if last and path:
            last[1] = found.end()


def x_length(text):
    """Characters as X counts them: any link is 23, most scripts 1, emoji and CJK 2.

    Agrees with twitter-text 3.1.0's parseTweet on 33,000 generated posts mixing file names,
    domains, paths, punctuation and symbols. The one known difference: an emoji sequence
    joined with U+200D is counted per code point, which errs long, and rules.md bans emoji.
    """
    text = unicodedata.normalize("NFC", text)
    length, cursor = 0, 0
    for start, end in [tuple(span) for span in x_links(text)] + [(len(text), len(text))]:
        length += sum(1 if any(low <= ord(ch) <= high for low, high in NARROW) else 2
                      for ch in text[cursor:start])
        length += X_LINK_LENGTH if end > start else 0
        cursor = end
    return length


# Each block name in rules.md section 7, the platform in config.json it belongs to, the
# key holding its length limit there, and how that platform counts characters.
BLOCKS = {
    "x-post": ("x", "max_chars", x_length),
    "x-reply": ("x", "max_chars", x_length),
    "reddit-title": ("reddit", "title_max_chars", len),
    "reddit-body": ("reddit", "body_max_chars", len),
    "instagram": ("instagram", "caption_max_chars", len),
    "linkedin": ("linkedin", "max_chars", len),
    "linkedin-comment": ("linkedin", "comment_max_chars", len),
}


def block_problems(kind, body, platforms):
    """What is wrong with one block, as sentences. Empty when it is fine."""
    if kind not in BLOCKS or BLOCKS[kind][0] not in platforms:
        return []
    platform, key, count = BLOCKS[kind]
    settings = platforms[platform]
    problems = []
    length, limit = count(body), settings.get(key)
    if limit is not None and length > limit:
        problems.append("Over the limit: %d of %d characters. Trim before posting." % (length, limit))
    tags, allowed = len(HASHTAG.findall(body)), settings.get("hashtags_max")
    if allowed is not None and tags > allowed:
        problems.append("%s: the rules allow %d." % (plural(tags, "hashtag"), allowed))
    return problems


def check_drafts(text, platforms):
    """Swap em dashes for commas and flag any post that breaks its platform's limits.

    Counting characters and hashtags is the part a model gets wrong, so it is done here
    and the result is written under the block it applies to.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")

    def annotate(found):
        kind, body, old_flags = found.group(1), found.group(2), found.group(3)
        if kind not in BLOCKS:
            return found.group(0)
        block = found.group(0)[:len(found.group(0)) - len(old_flags)]  # a re-run replaces old flags
        problems = block_problems(kind, body.strip(), platforms)
        return "\n".join([block] + ["> " + problem for problem in problems])

    return POST_BLOCK.sub(annotate, text)


# ----------------------------------------------------------------- self test

class FakeGitHub:
    """Canned answers keyed by path, so the tests never touch the network."""

    def __init__(self, pages, readmes=None):
        self.pages, self.readmes, self.calls = pages, readmes or {}, []

    def get(self, path, params=None, missing=None):
        self.calls.append(path)
        page = self.pages.get(path, missing)
        params = params or {}
        if path.endswith("/issues") and page:
            # As GitHub does: newest first, one page of per_page, pull requests included.
            page = sorted(page, key=lambda item: item["created_at"], reverse=True)
            page = page[:params.get("per_page", 30)]
        if path.endswith("/commits"):
            # As GitHub does: history from the ref asked for, since filtering on the
            # committer date, one page of per_page.
            page = self.pages.get("%s@%s" % (path, params.get("sha")), page)
            if page and params.get("since"):
                page = [c for c in page if parse_time((c["commit"].get("committer") or c["commit"]["author"])["date"])
                        >= parse_time(params["since"])]
            if page:
                page = page[:params.get("per_page", 30)]
        return page

    def get_all(self, path, params=None, max_pages=None):
        self.calls.append(path)
        page = self.pages.get("%s@%s" % (path, (params or {}).get("sha")), self.pages.get(path, []))
        return page if max_pages is None else page[:100 * max_pages]

    def readme(self, owner, repo):
        return self.readmes.get(repo, "")


def selftest():
    config = {
        "person": {"github": "me"},
        "issue": {"label": "post-drafts", "repo": "me/auto-post-"},
        "scan": {
            "public_only": True, "include_forks": False, "include_archived": False,
            "exclude_repos": ["Auto-Post-"], "ignore_authors": ["github-actions[bot]"],
            "start_from": "2026-09-01T00:00:00Z", "max_commits_per_repo": 3,
            "readme_chars": 20, "previous_drafts": 2, "previous_draft_chars": 50,
        },
    }
    t = parse_time
    failures, ran = [], []

    def check(name, got, want):
        ran.append(name)
        if got != want:
            failures.append("%s: got %r, want %r" % (name, got, want))

    def repo(name, pushed="2026-09-20T10:00:00Z", created="2026-01-01T00:00:00Z", **extra):
        return dict({"name": name, "full_name": "me/" + name, "owner": {"login": "me"},
                     "default_branch": "main", "pushed_at": pushed, "created_at": created,
                     "html_url": "https://github.com/me/" + name}, **extra)

    def commit(sha, message, login="me", parents=1, name="Me", date="2026-09-20T09:00:00Z", tree=None):
        # A rewritten copy keeps its original's author date and tree; give both the same tree.
        return {"sha": sha * 7, "html_url": "u", "parents": [{}] * parents,
                "author": {"login": login} if login else None,
                "commit": {"message": message, "author": {"name": name, "date": date},
                           "tree": {"sha": tree or "tree-" + sha}}}

    def issue(created, state=None, **extra):
        body = "drafts" + ("\n" + write_marker(state) if state is not None else "")
        return dict({"title": "Post drafts", "created_at": created, "body": body}, **extra)

    # the window
    check("no issues falls back to start_from",
          resolve_since(config, []), (t("2026-09-01T00:00:00Z"), "scan.start_from in config.json"))
    check("an issue without a marker counts from when it was opened",
          resolve_since(config, [issue("2026-09-10T07:00:00Z"), issue("2026-09-14T07:01:00Z")])[0],
          t("2026-09-14T07:01:00Z"))
    check("the marker's scan time beats the issue's creation time",
          resolve_since(config, [issue("2026-09-14T07:01:00Z", {"until": "2026-09-14T07:00:00Z"})])[0],
          t("2026-09-14T07:00:00Z"))
    check("start_from is a floor under an older issue",
          resolve_since(config, [issue("2026-08-01T07:00:00Z")])[0], t("2026-09-01T00:00:00Z"))
    check("a pull request never moves the window",
          resolve_since(config, [issue("2026-09-15T00:00:00Z", pull_request={})])[0],
          t("2026-09-01T00:00:00Z"))
    check("an override wins outright",
          resolve_since(config, [issue("2026-09-15T00:00:00Z")], "2026-08-01T00:00:00Z")[0],
          t("2026-08-01T00:00:00Z"))

    # the marker
    state = {"until": "2026-09-14T07:00:00Z", "heads": {"a": "abc", "b": 5}}
    check("marker round trips", read_marker("x " + write_marker(state) + " y"), state)
    check("a mangled marker reads as empty", read_marker("<!-- auto-post-state {nope} -->"), {})
    check("heads come from the newest issue and skip junk",
          previous_heads([issue("2026-09-10T00:00:00Z", {"heads": {"a": "old"}}),
                          issue("2026-09-14T00:00:00Z", state)]), {"a": "abc"})

    # which repositories are looked at
    since = t("2026-09-10T00:00:00Z")
    repos = [
        repo("public"), repo("secret", private=True), repo("forked", fork=True),
        repo("old", archived=True), repo("auto-post-"),
        repo("stale", pushed="2026-09-01T00:00:00Z"),
        dict(repo("theirs"), owner={"login": "someone-else"}),
    ]
    eligible, skipped = eligible_repos(repos, config)
    check("eligible, newest push first", [r["name"] for r in eligible], ["public", "stale"])
    check("private skipped", skipped["private"], 1)
    check("fork skipped", skipped["fork"], 1)
    check("archived skipped", skipped["archived"], 1)
    check("exclusion ignores case", skipped["excluded"], 1)
    check("a repository pushed before the window is not touched",
          touched_since(repo("stale", pushed="2026-09-01T00:00:00Z"), since), False)
    check("a repository created inside the window is touched",
          touched_since(repo("n", pushed="2026-09-01T00:00:00Z", created="2026-09-11T00:00:00Z"), since), True)

    # which commits count
    ignore = config["scan"]["ignore_authors"]
    check("ordinary commit kept", keep_commit(commit("a", "Add x"), ignore), True)
    check("merge dropped", keep_commit(commit("a", "Merge", parents=2), ignore), False)
    check("listed bot dropped", keep_commit(commit("a", "Brief", login="github-actions[bot]"), ignore), False)
    check("any [bot] name dropped", keep_commit(commit("a", "Bump", login=None, name="some[bot]"), ignore), False)
    check("commit with no linked account kept", keep_commit(commit("a", "Fix", login=None), ignore), True)

    # the digest end to end
    old_branch_work = commit("e", "Add the latency benchmark", date="2026-09-08T09:00:00Z")
    pages = {
        "/repos/me/auto-post-/issues": [
            issue("2026-09-14T07:01:00Z", {"until": "2026-09-14T07:00:00Z",
                                           "heads": {"merged": "m" * 7, "quiet": "q" * 7,
                                                     "rewritten": "r" * 7}}),
            issue("2026-09-16T07:00:00Z", pull_request={}),
        ],
        "/users/me/repos": [
            repo("results", pushed="2026-09-20T10:00:00Z"),
            repo("merged", pushed="2026-09-19T12:00:00Z"),
            repo("bots-only", pushed="2026-09-19T10:00:00Z"),
            repo("released", pushed="2026-09-18T10:00:00Z"),
            repo("rewritten", pushed="2026-09-17T10:00:00Z"),
            repo("fresh", pushed="2026-09-15T10:00:00Z", created="2026-09-15T09:00:00Z"),
            repo("quiet", pushed="2026-09-02T10:00:00Z"),
            repo("never-seen", pushed="2026-09-02T10:00:00Z"),
        ],
        "/repos/me/results/commits": [
            commit("a", "Report the 30 Hz result\n\nMeasured over 10 runs."),
            commit("b", "Merge branch", parents=2),
            commit("c", "Tidy"), commit("f", "Two"), commit("g", "Three"),
        ],
        # a branch written before the window and merged inside it: compare finds it,
        # a date filter would not
        "/repos/me/merged/compare/mmmmmmm...main": {"status": "ahead", "commits": [
            old_branch_work, commit("h", "Merge pull request #4", parents=2)]},
        "/repos/me/bots-only/commits": [commit("d", "Brief", login="github-actions[bot]")],
        "/repos/me/released/releases": [
            {"tag_name": "v1.0", "name": "First", "published_at": "2026-09-18T10:00:00Z",
             "body": "notes", "html_url": "r"},
            {"tag_name": "v0.9", "published_at": "2026-09-01T10:00:00Z", "html_url": "old"},
            {"tag_name": "v1.1", "draft": True, "published_at": None, "html_url": "draft"},
        ],
        "/repos/me/released/commits": [],
        # history rewritten: compare says diverged, so fall back to the dated list
        # history rewritten: the copies of old commits keep their author dates and trees
        "/repos/me/rewritten/compare/rrrrrrr...main": {"status": "diverged", "commits": [
            commit(str(i), "old %d, trailer stripped" % i, date="2026-01-0%dT00:00:00Z" % (i + 1), tree="t%d" % i)
            for i in range(9)] + [commit("k", "Rewrite trailers out")]},
        "/repos/me/rewritten/commits@rrrrrrr": [
            commit(chr(97 + i), "old %d" % i, date="2026-01-0%dT00:00:00Z" % (i + 1), tree="t%d" % i)
            for i in reversed(range(9))],
        "/repos/me/never-seen/commits": [commit("n", "Initial")],
    }
    github = FakeGitHub(pages, {"results": "R" * 30})
    digest = build_digest(github, config, now=t("2026-09-21T07:00:00Z"))
    names = [r["name"] for r in digest["repos"]]
    by_name = {r["name"]: r for r in digest["repos"]}
    check("window starts at the last scan, not the pull request",
          digest["since"], "2026-09-14T07:00:00Z")
    check("repos with something to say", names, ["results", "merged", "released", "rewritten", "fresh"])
    check("a merged branch is found through the recorded head",
          [c["subject"] for c in by_name["merged"]["commits"]], ["Add the latency benchmark"])
    check("and is labelled as such", by_name["merged"]["found_by"], "since last head")
    check("a rewritten history is recognised by identity and only the new commit is kept",
          ([c["subject"] for c in by_name["rewritten"]["commits"]], by_name["rewritten"]["found_by"]),
          (["Rewrite trailers out"], "since last head, history rewritten"))
    check("merge left out, list capped and counted",
          ([c["subject"] for c in by_name["results"]["commits"]], by_name["results"]["commit_count"],
           by_name["results"]["commits_capped"]),
          (["Report the 30 Hz result", "Tidy", "Two"], 4, True))
    check("commit body kept", by_name["results"]["commits"][0]["body"], "Measured over 10 runs.")
    check("only the published release inside the window",
          [r["tag"] for r in by_name["released"]["releases"]], ["v1.0"])
    check("readme cut and marked", by_name["results"]["readme"], "R" * 20 + TRUNCATED)
    check("a new repository with no commits still counts", by_name["fresh"]["is_new"], True)
    check("unchanged covers bot-only and unpushed repositories", digest["skipped"]["unchanged"], 3)
    # after a merged pull request the branch head is the merge commit itself
    check("new heads recorded, old ones carried, first sightings looked up",
          {k: digest["state"]["heads"].get(k) for k in ("results", "merged", "rewritten", "quiet", "never-seen")},
          {"results": "a" * 7, "merged": "h" * 7, "rewritten": "k" * 7, "quiet": "q" * 7,
           "never-seen": "n" * 7})
    check("the marker is stripped from previous drafts", digest["previous_drafts"][0]["body"], "drafts")
    earlier = issue("2026-09-14T07:01:00Z", {"until": "2026-09-14T07:00:00Z", "heads": {"a": "old"}})
    newest = issue("2026-09-21T07:02:00Z", {"until": "2026-09-21T07:00:00Z", "heads": {"a": "new"}})
    check("a scan from an earlier point compares from the heads current then",
          (previous_heads([earlier, newest]), previous_heads([earlier, newest], before=t("2026-09-14T07:00:00Z")),
           previous_heads([earlier, newest], before=t("2026-09-01T00:00:00Z"))),
          ({"a": "new"}, {"a": "old"}, {}))
    replay = dict(pages)
    replay["/repos/me/auto-post-/issues"] = [
        issue("2026-09-21T07:02:00Z", digest["state"])] + pages["/repos/me/auto-post-/issues"]
    again = build_digest(FakeGitHub(replay, {"results": "R" * 30}), config, override=digest["since"],
                         now=t("2026-09-21T08:00:00Z"))
    check("the recovery command in an oversized issue reproduces its digest",
          [r["name"] for r in again["repos"]], names)
    crowded_issues = [issue("2026-09-2%dT07:00:00Z" % i, {"until": "2026-09-2%dT06:59:00Z" % i, "heads": {}},
                            pull_request={}) for i in range(1, 10)]
    paged = dict(replay)
    paged["/repos/me/auto-post-/issues"] = replay["/repos/me/auto-post-/issues"] + crowded_issues
    again = build_digest(FakeGitHub(paged, {"results": "R" * 30}), config, override=digest["since"],
                         now=t("2026-09-21T08:00:00Z"))
    check("recovery still finds the right heads when newer issues and pull requests crowd the page",
          [(r["name"], r["found_by"], [c["subject"] for c in r["commits"]]) for r in again["repos"]],
          [(r["name"], r["found_by"], [c["subject"] for c in r["commits"]]) for r in digest["repos"]])
    carry = dict(pages)
    carry["/repos/me/auto-post-/issues"] = [
        issue("2026-09-10T07:01:00Z", {"until": "2026-09-10T07:00:00Z", "heads": {"quiet": "q" * 7}}),
        issue("2026-09-14T07:01:00Z", {"until": "2026-09-14T07:00:00Z", "heads": {"quiet": "Q" * 7}})]
    carried = build_digest(FakeGitHub(carry), config, override="2026-09-12T00:00:00Z", now=t("2026-09-21T07:00:00Z"))
    check("a --since run carries an untouched repository's newest head, not an older one",
          carried["state"]["heads"]["quiet"], "Q" * 7)
    check("the next window starts where this scan ended",
          resolve_since(config, [issue("2026-09-21T07:02:00Z", digest["state"])])[0],
          t("2026-09-21T07:00:00Z"))

    # history rewrites, new repositories, releases on old tags, the start_from floor, races
    def subjects(result):
        return [c["subject"] for c in result["repos"][0]["commits"]] if result["repos"] else ["<no repository>"]

    def world(extra_pages, repos_list, issues_list):
        pages_w = dict(extra_pages)
        pages_w["/users/me/repos"] = repos_list
        pages_w["/repos/me/auto-post-/issues"] = issues_list
        return FakeGitHub(pages_w)

    last = issue("2026-09-25T07:35:00Z", {"until": "2026-09-25T07:34:00Z", "heads": {"proj": "p" * 7}})
    old_p = commit("p", "Tune the controller", date="2026-09-24T10:00:00Z")
    amended = commit("P", "Tune the controller, reworded", date="2026-09-24T10:00:00Z", tree="tree-p")
    pr_work = commit("w", "Add the grasp benchmark", date="2026-09-24T12:00:00Z")
    merge = commit("m", "Merge pull request #7", parents=2, date="2026-09-26T10:00:00Z")
    rewritten = build_digest(world({
        "/repos/me/proj/compare/ppppppp...main": {"status": "diverged", "total_commits": 3,
                                                  "commits": [amended, pr_work, merge]},
        "/repos/me/proj/commits@ppppppp": [old_p],
    }, [repo("proj", pushed="2026-09-26T10:00:00Z")], [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("a force-push in the same window as a merge keeps the merged work and drops rewritten copies",
          (subjects(rewritten), rewritten["state"]["heads"].get("proj")),
          (["Add the grasp benchmark"], "m" * 7))

    local = [commit("z", "Write the README", date="2026-09-26T12:00:00Z"),
             commit("y", "Success rate 81% over 120 trials", date="2026-09-21T09:00:00Z")]
    pushed_whole = build_digest(world({"/repos/me/grasp-sim/commits": local},
                                      [repo("grasp-sim", pushed="2026-09-26T12:00:00Z", created="2026-09-26T11:00:00Z")],
                                      [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("a new repository pushed with its local history is read whole",
          subjects(pushed_whole),
          ["Write the README", "Success rate 81% over 120 trials"])
    empty_first = build_digest(world({"/repos/me/grasp-sim/commits": local},
                                     [repo("grasp-sim", pushed="2026-09-26T12:00:00Z", created="2026-09-24T11:00:00Z")],
                                     [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("a repository first seen empty is read whole once it has history",
          len(subjects(empty_first)), 2)

    tagged = build_digest(world({"/repos/me/proj/releases": [
        {"tag_name": "v1.0", "name": "v1.0", "published_at": "2026-09-25T12:00:00Z", "html_url": "r"}],
        "/repos/me/proj/compare/ppppppp...main": {"status": "identical", "total_commits": 0, "commits": []}},
        [repo("proj", pushed="2026-09-25T07:00:00Z")], [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("a release published from a tag pushed earlier is still reported",
          [r["tag"] for r in tagged["repos"][0]["releases"]] if tagged["repos"] else [], ["v1.0"])

    floor_config = json.loads(json.dumps(config))
    floor_config["scan"]["start_from"] = "2026-09-30T00:00:00Z"
    floored = build_digest(world({
        "/repos/me/proj/compare/ppppppp...main": {"status": "ahead", "total_commits": 2, "commits": [
            commit("o", "Old experiment, skip me", date="2026-09-26T09:00:00Z"),
            commit("n", "New work after the break", date="2026-10-01T09:00:00Z")]},
        "/repos/me/proj/commits": [commit("n", "New work after the break", date="2026-10-01T09:00:00Z"),
                                   commit("o", "Old experiment, skip me", date="2026-09-26T09:00:00Z")]},
        [repo("proj", pushed="2026-10-01T09:00:00Z")], [last]), floor_config, now=t("2026-10-02T07:34:00Z"))
    check("a start_from after the last scan is a real floor",
          (floored["since"], subjects(floored)),
          ("2026-09-30T00:00:00Z", ["New work after the break"]))

    raced = build_digest(world({"/repos/me/proj/releases": [
        {"tag_name": "v2", "name": "v2", "published_at": "2026-09-28T07:34:20Z", "html_url": "r"}],
        "/repos/me/proj/compare/ppppppp...main": {"status": "identical", "total_commits": 0, "commits": []}},
        [repo("proj", pushed="2026-09-25T07:00:00Z"),
         repo("late", pushed="2026-09-28T07:34:10Z", created="2026-09-28T07:34:10Z")], [last]),
        config, now=t("2026-09-28T07:34:00Z"))
    check("a release or repository appearing during the scan waits for the next window",
          [r["name"] for r in raced["repos"]], [])

    big = build_digest(world({
        "/repos/me/proj/compare/ppppppp...main": {"status": "ahead", "total_commits": 300,
                                                  "commits": [commit("s", "step %d" % i) for i in range(250)]},
        "/repos/me/proj/commits@main": [commit("T", "the tip")] + [commit("s%d" % i, "step %d" % i)
                                                                    for i in range(298, 199, -1)],
        "/repos/me/proj/commits@ppppppp": [commit("p", "before")]},
        [repo("proj", pushed="2026-09-26T10:00:00Z")], [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("more than a page of new commits records the real tip and says the count is a lower bound",
          ((big["repos"] or [{}])[0].get("commit_count"), (big["repos"] or [{}])[0].get("count_complete"),
           big["state"]["heads"].get("proj")),
          (100, False, "T" * 7))

    root = commit("r", "Start the planner", date="2026-09-10T09:00:00Z", tree="t-root")
    history_old = [old_p, root]
    root_copy = commit("R", "Start the planner (trailers stripped)", date="2026-09-10T09:00:00Z", tree="t-root")
    p_copy = commit("Q", "Tune the controller", date="2026-09-24T10:00:00Z", tree="tree-p")
    pr_early = commit("e", "Grasp bench: 120 trials", date="2026-09-24T09:00:00Z")
    pr_late = commit("f", "Grasp bench: 81% success", date="2026-09-24T19:00:00Z")
    merged_after = commit("M", "Merge pull request #9", parents=2, date="2026-09-26T10:00:00Z")
    reroot = build_digest(world({  # no compare page: GitHub answers 404, no common ancestor
        "/repos/me/proj/commits@main": [merged_after, pr_late, pr_early, p_copy, root_copy],
        "/repos/me/proj/commits@ppppppp": history_old,
    }, [repo("proj", pushed="2026-09-26T12:00:00Z")], [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("a rewrite of the root commit keeps the work merged in the same window, and only that",
          (subjects(reroot), reroot["state"]["heads"].get("proj")),
          (["Grasp bench: 81% success", "Grasp bench: 120 trials"], "M" * 7))

    reexposed = build_digest(world({  # a PR cut before the rewrite, merged after: compare says ahead
        "/repos/me/proj/compare/ppppppp...main": {"status": "ahead", "total_commits": 3,
                                                  "commits": [p_copy, pr_work, merge]},
        "/repos/me/proj/commits@ppppppp": history_old,
    }, [repo("proj", pushed="2026-09-26T10:00:00Z")], [last]), config, now=t("2026-09-28T07:34:00Z"))
    check("rewritten copies brought back by a merge are not reported again",
          subjects(reexposed), ["Add the grasp benchmark"])

    readme_first = issue("2026-09-25T07:35:00Z", {"until": "2026-09-25T07:34:00Z", "heads": {"grasp-sim": "i" * 7}})
    replaced = build_digest(world({  # GitHub made a README commit; the local history was force-pushed over it
        "/repos/me/grasp-sim/commits@main": local,
        "/repos/me/grasp-sim/commits@iiiiiii": [commit("i", "Initial commit", date="2026-09-24T11:00:00Z")],
    }, [repo("grasp-sim", pushed="2026-09-26T12:00:00Z", created="2026-09-24T11:00:00Z")], [readme_first]),
        config, now=t("2026-09-28T07:34:00Z"))
    check("local history force-pushed over a new repository's first commit is reported",
          subjects(replaced), ["Write the README", "Success rate 81% over 120 trials"])

    renamed_issue = issue("2026-09-25T07:35:00Z", {"until": "2026-09-25T07:34:00Z", "heads": {"42": "z" * 7}})
    renamed = build_digest(world({
        "/repos/me/new-name/compare/zzzzzzz...main": {"status": "identical", "total_commits": 0, "commits": []},
        "/repos/me/new-name/commits": local,
    }, [repo("new-name", id=42, pushed="2026-09-26T12:00:00Z", created="2026-09-20T11:00:00Z")],
        [renamed_issue]), config, now=t("2026-09-28T07:34:00Z"))
    check("renaming a repository does not make it read whole again",
          ([r["name"] for r in renamed["repos"]], renamed["state"]["heads"]), ([], {"42": "z" * 7}))

    long_history = [commit("h%d" % i, "step %d" % i, date="2026-09-26T%02d:%02d:00Z" % (i // 60, i % 60))
                    for i in range(149, -1, -1)]
    deep = build_digest(world({"/repos/me/deep/commits@main": long_history},
                              [repo("deep", pushed="2026-09-26T12:00:00Z", created="2026-09-26T11:00:00Z")], [last]),
                        config, now=t("2026-09-28T07:34:00Z"))
    check("a new repository with more than a page of history is counted in full",
          ((deep["repos"] or [{}])[0].get("commit_count"), (deep["repos"] or [{}])[0].get("count_complete")),
          (150, True))

    recommitted = dict(commit("o", "Old work, rebased later", date="2026-08-20T09:00:00Z"))
    recommitted["commit"]["committer"] = {"name": "Me", "date": "2026-09-26T09:00:00Z"}
    first_run = build_digest(world({"/repos/me/proj/commits": [commit("n", "New work", date="2026-09-26T10:00:00Z"),
                                                                recommitted]},
                                   [repo("proj", pushed="2026-09-26T10:00:00Z")], []),
                             config, now=t("2026-09-28T07:34:00Z"))
    check("on a first run, work authored before the window but re-committed inside it stays out",
          subjects(first_run), ["New work"])

    markerless = dict(issue("2026-09-27T07:00:00Z"), body="edited by hand")
    check("an issue whose marker was edited away does not hide the heads before it",
          previous_heads([last, markerless]), {"proj": "p" * 7})

    # the rendered digest and the issue body
    text = render(digest)
    check("render names every repository", all(name in text for name in names), True)
    check("render carries no em dash", "\u2014" in text, False)
    body = compose(digest, "1. a post")
    check("the issue body starts with the marker", read_marker(body.split("\n", 1)[0]), digest["state"])
    bare = compose(digest, "")
    embedded = json.loads(bare.split("```json\n", 1)[1].split("\n```", 1)[0])
    check("no drafts: the body carries the digest an assistant needs, minus the marker",
          ("rules.md" in bare and "config.json" in bare, embedded["repos"][0]["commits"][0]["body"],
           "state" in embedded), (True, "Measured over 10 runs.", False))
    huge = json.loads(json.dumps(digest))
    for repo in huge["repos"]:
        repo["readme"] = "R" * 50000
    squeezed = compose(huge, "", limit=20000)
    squeezed_json = json.loads(squeezed.split("```json\n", 1)[1].split("\n```", 1)[0])
    check("an oversized digest shrinks its readmes, stays valid JSON and keeps the marker",
          (len(squeezed) <= 20000, read_marker(squeezed) == huge["state"],
           len(squeezed_json["repos"][0]["readme"]) < 50000), (True, True, True))
    busy = json.loads(json.dumps(digest))
    busy["repos"] = [dict(busy["repos"][0], name="r%d" % i, commits_capped=False,
                          commits=[dict(busy["repos"][0]["commits"][0], body="b" * 600)] * 40)
                     for i in range(8)]
    crowded = compose(busy, "", limit=20000)
    crowded_json = json.loads(crowded.split("```json\n", 1)[1].split("\n```", 1)[0])
    check("8 repos of 40 long commits: the digest still fits, parses and says it was cut",
          (len(crowded) <= 20000, crowded.rstrip().endswith("</details>"),
           crowded_json["repos"][0]["commits_capped"], TRUNCATED in crowded), (True, True, True, False))
    wide = json.loads(json.dumps(digest))
    wide["repos"] = [dict(wide["repos"][0], name="repo-%d" % i, description="d" * 350,
                          commits=[dict(wide["repos"][0]["commits"][0], subject="s" * 70)] * 40,
                          commit_count=40) for i in range(20)]
    for limit, drafts in ((60000, ""), (20000, ""), (5000, ""), (60000, "x" * 1000), (8000, "y" * 9000)):
        body = compose(wide, drafts, limit=limit)
        closed = body.count("<details>") == body.count("</details>")
        check("20 busy repos, limit %d, %s: fits, blocks closed, marker whole"
              % (limit, "drafts" if drafts else "no drafts"),
              (len(body) <= limit, closed, read_marker(body) == wide["state"]), (True, True, True))
    check("a shortened render says what it left out",
          ("and 37 more" in render_within(wide, 20000), "more with changes" in render_within(wide, 3000)),
          (True, True))
    week = json.loads(json.dumps(digest))
    week["repos"] = [dict(week["repos"][0], name="repo-%d" % i, readme="R" * 6000, commits_capped=False,
                          commits=[dict(week["repos"][0]["commits"][0], subject="s" * 70)] * 40, commit_count=40)
                     for i in range(7)]
    full = compose(week, "")
    check("with room to spare the person's list keeps every commit",
          ("more" in full.split("Digest for an assistant")[0], len(full) <= ISSUE_LIMIT), (False, True))
    odd = json.loads(json.dumps(digest))
    odd["repos"][0]["description"] = "```json"
    odd["repos"][0]["commits"][0]["subject"] = "Remove the stray </details> from the FAQ " + "x" * 300
    odd["repos"][0]["releases"] = [{"tag": "v1", "name": "Fold the log into a <details> block",
                                    "published_at": "2026-09-20T00:00:00Z", "body": "</details>", "url": "u"}]
    for drafts in ("", "1. a post"):
        body = compose(odd, drafts)
        fences = [line for line in body.split("\n") if line.startswith("```")]
        parsed = True
        if not drafts:
            try:
                json.loads(body.split("\n```json\n", 1)[1].split("\n```", 1)[0])
            except ValueError:
                parsed = False
        check("tags and fences inside commit text cannot break the issue (%s)" % ("drafts" if drafts else "no drafts"),
              (body.count("<details>"), body.count("</details>"), len(fences) % 2, parsed),
              (2 if not drafts else 1, 2 if not drafts else 1, 0, True))
    fancy = json.loads(json.dumps(digest))
    fancy["repos"][0]["description"] = "~~~ tilde notes"
    fancy["repos"][0]["releases"] = [{"tag": "v2", "name": "[Beta] v2.0\\", "published_at": "x", "body": "", "url": "u"}]
    fancy["repos"][0]["commits"][0]["subject"] = "Use `scan.py` for the R&D check"
    marked, plain = render(fancy, markdown=True), render(fancy)
    check("Markdown escaping in the issue, plain text in the log",
          ("\\~\\~\\~ tilde notes" in marked, "- release [\\[Beta\\] v2.0\\\\](u)" in marked,
           "Use \\`scan.py\\` for the R&amp;D check" in marked, "Use `scan.py` for the R&D check" in plain,
           "~~~ tilde notes" in plain),
          (True, True, True, True, True))
    long_line = render(odd).split("\n")
    check("a long commit subject stays on one line, shortened with an ellipsis",
          [line for line in long_line if "stray" in line][0].endswith("x..."), True)
    crowded_state = dict(digest["state"], heads={"repo-%04d" % i: "a" * 40 for i in range(28)})
    near = dict(digest, state=crowded_state)
    squeezed_drafts = [compose(near, "x" * size, limit=len(write_marker(crowded_state)) + extra)
                       for size in (500, 70000) for extra in (200, 210, 230)]
    check("drafts are cut to nothing, not kept whole, when the marker leaves almost no room",
          [len(body) <= len(write_marker(crowded_state)) + extra
           for body, extra in zip(squeezed_drafts, (200, 210, 230) * 2)], [True] * 6)
    unclosed = compose(digest, "```x-post\nA post the model never closed")
    check("drafts that leave a fence open are closed before the list",
          sum(1 for line in unclosed.split("\n") if line.startswith("```")) % 2, 0)
    before_list = unclosed.split("<details><summary>What changed")[0]
    check("an open fence in the drafts is closed before the list, not after it",
          sum(1 for line in before_list.split("\n") if line.startswith("```")) % 2, 0)
    three = json.loads(json.dumps(digest))
    three["previous_drafts"] = [{"title": "p", "created_at": "x", "body": "d" * 6000}] * 2
    three["repos"] = [dict(three["repos"][0], name="repo-%d" % i, readme="R" * 6000, commits_capped=False,
                           commits=[dict(three["repos"][0]["commits"][0], subject="s" * 50, body="b" * 300)] * 40,
                           commit_count=40) for i in range(3)]
    kept_whole = compose(three, "")
    check("without drafts the list keeps every commit when a shorter digest fits beside it",
          ("more" in kept_whole.split("Digest for an assistant")[0], "```json" in kept_whole), (False, True))
    ideal = compose(three, "", limit=10 ** 6)
    exact = compose(three, "", limit=len(ideal))
    check("a body that fits exactly is not shortened", exact, ideal)
    cut = compose(digest, "```x-post\n" + "word " * 2000 + "\n```", limit=5000)
    check("drafts cut to fit never leave a code fence open",
          sum(1 for line in cut.split("\n") if line.startswith("```")) % 2, 0)
    tiny = compose(busy, "", limit=3000)
    check("a digest that cannot fit is left out and the note says how to get it",
          ("```json" in tiny, "too large to include" in tiny, read_marker(tiny) == busy["state"]),
          (False, True, True))
    short = compose(digest, "x" * 5000, limit=1000)
    check("an oversized body is trimmed but keeps its marker",
          (len(short) <= 1000, read_marker(short) == digest["state"]), (True, True))
    empty = build_digest(FakeGitHub({}), config, now=t("2026-09-21T07:00:00Z"))
    check("nothing new means has_changes is false", empty["has_changes"], False)

    # checking drafts
    check("plain text counts one per character", x_length("abc def"), 7)
    check("a link counts 23 whatever its length", x_length("see https://github.com/me/a-very-long-name"), 27)
    check("emoji count two", x_length("\U0001F916"), 2)
    # Each pair was measured with twitter-text 3.1.0 parseTweet, the library X counts with.
    measured = [
        ("see cicatrixa.com", 27), ("see train.py", 27), ("see README.md now", 31), ("scan.py", 23),
        ("np.dot", 23), ("model.pt", 23), ("run.sh", 23), ("foo.io", 23), ("see x.io", 27),
        ("abyyworld.github.io/teleop-pipeline/", 23), ("www.akbarjuraev.com", 23),
        ("https://github.com/abyyworld/vla-evals", 23), ("config.json", 11), ("plot.png", 8),
        ("sim.mp4", 7), ("rclpy.spin", 10), ("v0.1.0", 6), ("e.g. this", 9),
        ("./policy-evals run", 18), ("3.2 ms \u2192 1.1 ms", 16), ("Isaac Lab.", 10),
        ("see configure.ac", 27), ("see foo.onion", 27),
    ]
    check("counts agree with twitter-text", [(t, x_length(t)) for t, _ in measured], measured)
    platforms = {
        "x": {"max_chars": 280, "hashtags_max": 0},
        "reddit": {"title_max_chars": 300, "body_max_chars": 1000},
        "instagram": {"caption_max_chars": 2200, "hashtags_max": 5},
        "linkedin": {"max_chars": 3000, "hashtags_max": 3},
    }

    def block(kind, body):
        return "```%s\n%s\n```" % (kind, body)

    checked = check_drafts("Intro \u2014 here.\n\n" + block("x-post", "a" * 281) + "\n\n"
                           + block("x-reply", "Code: https://github.com/me/" + "b" * 300) + "\n", platforms)
    check("an over-long X post is flagged", "> Over the limit: 281 of 280" in checked, True)
    check("a long link does not trip the X limit", checked.count("Over the limit"), 1)
    check("em dashes become commas", ("\u2014" in checked, "Intro, here." in checked), (False, True))
    check("a LinkedIn post is measured against LinkedIn, not X",
          "Over the limit" in check_drafts(block("linkedin", "a" * 900), platforms), False)
    check("an over-long LinkedIn post is flagged",
          "3001 of 3000" in check_drafts(block("linkedin", "a" * 3001), platforms), True)
    platforms["linkedin"]["comment_max_chars"] = 1250
    check("a LinkedIn first comment is measured against the comment limit",
          "1251 of 1250" in check_drafts(block("linkedin-comment", "a" * 1251), platforms), True)
    check("a Reddit title is measured on its own",
          ("301 of 300" in check_drafts(block("reddit-title", "t" * 301), platforms),
           "Over" in check_drafts(block("reddit-body", "b" * 999), platforms)), (True, False))
    check("an Instagram caption is measured", "2201 of 2200" in check_drafts(block("instagram", "c" * 2201), platforms), True)
    tagged = check_drafts(block("instagram", "Grasping in sim. #robotics #ros2 #mujoco #ai #ml #cv"), platforms)
    check("too many hashtags are flagged", "> 6 hashtags: the rules allow 5." in tagged, True)
    check("any hashtag on X is flagged when none are allowed",
          "1 hashtag: the rules allow 0." in check_drafts(block("x-post", "Result. #robotics"), platforms), True)
    check("an issue number, a URL fragment and C# are not hashtags",
          HASHTAG.findall("PR #4, see https://x.com/a#b, written in C#"), [])
    check("an unknown block or an unconfigured platform is left alone",
          check_drafts(block("text", "a" * 5000) + block("instagram", "a" * 5000), {"x": {"max_chars": 280}}),
          block("text", "a" * 5000) + block("instagram", "a" * 5000))
    once = check_drafts(block("x-post", "a" * 300) + "\n", platforms)
    check("running check twice changes nothing", check_drafts(once, platforms), once)
    fixed = once.replace("a" * 300, "a" * 200)
    check("a fixed post loses its old flag on the next run", "Over the limit" in check_drafts(fixed, platforms), False)
    crlf = (block("x-post", "a" * 300) + "\n" + block("instagram", "#a #b #c #d #e #f")).replace("\n", "\r\n")
    checked_crlf = check_drafts(crlf, platforms)
    check("CRLF text is checked like LF text", ("300 of 280" in checked_crlf, "6 hashtags" in checked_crlf),
          (True, True))
    tail = check_drafts(block("x-post", "word #ros2"), platforms)
    check("a flagged block at the very end stays put on a re-run", check_drafts(tail, platforms), tail)
    quoted = block("reddit-body", "Our checker printed this:\n> 2 hashtags: the rules allow 0.\nThat is how it works.")
    check("a flag quoted inside a post is left alone", check_drafts(quoted + "\n", platforms), quoted + "\n")
    after_code = block("python", "print(1)") + "\n> Over the limit: 1 of 280 characters. Trim before posting.\n"
    check("a flag-like line after a non-post block is left alone", check_drafts(after_code, platforms), after_code)
    two = check_drafts(block("x-post", "a" * 300 + " #one"), platforms)
    check("every problem gets its own line", (two.count("\n> "), two.endswith("allow 0.")), (2, True))

    if failures:
        print("FAIL %d of %d" % (len(failures), len(ran)))
        for failure in failures:
            print("  " + failure)
        return 1
    print("ok, %d checks passed" % len(ran))
    return 0


# ----------------------------------------------------------------- command line

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("since", help="print when the current window starts, and why")

    scan = commands.add_parser("scan", help="build the digest of what changed")
    scan.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    scan.add_argument("--since", help="start the window here instead, ISO 8601")
    scan.add_argument("--out", help="also write the JSON digest to this file")

    show = commands.add_parser("render", help="print a saved JSON digest as Markdown")
    show.add_argument("digest")

    body = commands.add_parser("compose", help="print the issue body for a saved digest")
    body.add_argument("digest")
    body.add_argument("--drafts", help="file holding the drafts; omit when there are none")

    measure = commands.add_parser("check", help="flag posts over their platform's limits, drop em dashes")
    measure.add_argument("drafts")

    commands.add_parser("selftest", help="run the built in tests")
    args = parser.parse_args(argv)

    if args.command == "selftest":
        return selftest()
    if args.command in ("render", "compose"):
        digest = json.loads(Path(args.digest).read_text(encoding="utf-8"))
        if args.command == "render":
            print(render(digest), end="")
        else:
            drafts = Path(args.drafts).read_text(encoding="utf-8") if args.drafts else None
            print(compose(digest, drafts), end="")
        return 0

    config = load_config(args.config)
    if args.command == "check":
        print(check_drafts(Path(args.drafts).read_text(encoding="utf-8"),
                           config["posts"]["platforms"]), end="")
        return 0

    github = GitHub(token_from_env())

    if args.command == "since":
        moment, reason = resolve_since(config, fetch_draft_issues(github, config))
        print("%s  (%s)" % (format_time(moment), reason))
        return 0

    digest = build_digest(github, config, override=args.since)
    if args.out:
        Path(args.out).write_text(json.dumps(digest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(digest, indent=2) if args.json else render(digest), end="\n" if args.json else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
