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
commits were written still shows up. With no marker to go on (the first run, or a head
that was rewritten away) it falls back to commits dated inside the window. The window
never starts before scan.start_from in config.json.

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

    def get_all(self, path, params=None):
        """Every page, following the Link header."""
        items, url, query = [], path, dict(params or {}, per_page=100)
        while url:
            body, link = self._request(url, query)
            items.extend(json.loads(body))
            url, query = next_link(link), None
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
    """Head commits recorded by the newest issue, by repository name.

    With before, the newest issue whose scan ran at or before that moment, so a scan
    started from an earlier point compares from the heads that were current then.
    """
    issues = draft_issues(issues)
    if before is not None:
        issues = [issue for issue in issues if issue_until(issue) <= before]
    if not issues:
        return {}
    latest = max(issues, key=lambda issue: parse_time(issue["created_at"]))
    heads = read_marker(latest.get("body")).get("heads") or {}
    return {name: sha for name, sha in heads.items() if isinstance(sha, str)}


def fetch_draft_issues(github, config, count):
    return draft_issues(github.get("/repos/%s/issues" % config["issue"]["repo"], {
        "labels": config["issue"]["label"],
        "state": "all",
        "sort": "created",
        "direction": "desc",
        "per_page": max(count, 1),
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


def new_commits(github, repo, since, previous):
    """Return (commits newest first, head sha, how they were found).

    From the previous head when there is one and it is still an ancestor of the
    branch. Otherwise, commits dated inside the window.
    """
    if previous:
        compared = github.get("/repos/%s/compare/%s...%s"
                              % (repo["full_name"], previous, repo["default_branch"]))
        if compared and compared.get("status") in ("ahead", "identical"):
            commits = list(reversed(compared.get("commits") or []))
            return commits, (commits[0]["sha"] if commits else previous), "since last head"

    commits = github.get("/repos/%s/commits" % repo["full_name"], {
        "sha": repo["default_branch"],
        "since": format_time(since),
        "per_page": 100,
    }, missing=[]) or []
    head = commits[0]["sha"] if commits else current_head(github, repo)
    return commits, head, "dated in window"


def repo_changes(github, repo, config, since, previous):
    """Return (what happened in the window or None, the head to remember)."""
    scan = config["scan"]
    limit = scan["max_commits_per_repo"]
    raw, head, method = new_commits(github, repo, since, previous)
    kept = [summarise_commit(c) for c in raw if keep_commit(c, scan["ignore_authors"])]

    releases = []
    for release in github.get("/repos/%s/releases" % repo["full_name"],
                              {"per_page": 10}, missing=[]) or []:
        published = parse_time(release.get("published_at"))
        if release.get("draft") or not published or published < since:
            continue
        releases.append({
            "tag": release.get("tag_name"),
            "name": release.get("name") or release.get("tag_name"),
            "published_at": release.get("published_at"),
            "body": clip(release.get("body"), 2000),
            "url": release.get("html_url"),
        })

    is_new = (parse_time(repo.get("created_at")) or since) >= since
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
        "commit_count": len(kept),
        "commits_capped": len(kept) > limit,
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
    issues = fetch_draft_issues(github, config, scan["previous_drafts"])
    since, reason = resolve_since(config, issues, override)
    heads = previous_heads(issues, before=since if override else None)

    eligible, skipped = eligible_repos(list_repos(github, config), config)
    skipped["unchanged"] = 0
    repos, new_heads = [], {}
    for repo in eligible:
        name = repo["name"]
        if touched_since(repo, since):
            changes, head = repo_changes(github, repo, config, since, heads.get(name))
            if changes:
                repos.append(changes)
            else:
                skipped["unchanged"] += 1
        else:
            # Nothing pushed, so the recorded head still stands. A repository seen for
            # the first time gets one lookup, so the next run can compare against it.
            head = heads.get(name) or current_head(github, repo)
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


def render(digest, max_commits=None, max_repos=None):
    """The digest as Markdown. max_commits and max_repos shorten it, saying what was left out."""
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
        tags.append(plural(repo["commit_count"], "commit"))
        if repo["releases"]:
            tags.append(plural(len(repo["releases"]), "release"))
        lines.append("### [%s](%s)  (%s)" % (repo["name"], repo["url"], ", ".join(tags)))
        if repo["description"]:
            lines.append(clip(repo["description"], 300))
        for release in repo["releases"]:
            lines.append("- release [%s](%s)" % (clip(release["name"], 200), release["url"]))
        commits = repo["commits"] if max_commits is None else repo["commits"][:max_commits]
        for commit in commits:
            lines.append("- `%s` %s" % (commit["sha"], clip(commit["subject"], 200)))
        if repo["commit_count"] > len(commits):
            lines.append("- and %d more" % (repo["commit_count"] - len(commits)))
        lines.append("")
    if len(shown) < len(digest["repos"]):
        lines.append("And %s more with changes." % plural(len(digest["repos"]) - len(shown), "repository"))
        lines.append("")
    skipped = ", ".join("%d %s" % (value, key) for key, value in digest["skipped"].items() if value)
    if skipped:
        lines.append("Not in this digest: %s." % skipped)
    return "\n".join(lines).rstrip() + "\n"


def render_within(digest, limit):
    """render(), shortened until it fits in limit characters: fewer commits, then fewer repos."""
    for max_commits in (None, 10, 3, 0):
        text = render(digest, max_commits)
        if len(text) <= limit:
            return text
    count = len(digest["repos"])
    while count > 0:
        count //= 2
        text = render(digest, 0, count)
        if len(text) <= limit:
            return text
    return None


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
        if len(text) <= limit:
            return text
    return None


def compose(digest, drafts=None, limit=ISSUE_LIMIT):
    """The issue body, at most limit characters. The marker goes first and is never cut.

    Without drafts, the body carries the digest itself, so any assistant given it with
    rules.md and config.json has every fact the rules require a claim to trace to. Each
    part is shortened to fit rather than cut through, so every block stays closed.
    """
    marker = write_marker(digest["state"])
    if len(marker) + 200 > limit:
        raise ValueError("the state marker alone is %d characters, over the %d limit"
                         % (len(marker), limit))
    details = "<details><summary>%s</summary>\n\n%s\n</details>"
    if drafts and drafts.strip():
        head = drafts.strip()
        room = limit - len(marker) - 200
        if len(head) > room:
            head = head[:room - len(TRUNCATED)].rstrip() + TRUNCATED
        parts = [marker, head]
    else:
        head = ("No drafts this time: no model was available to write them. To get them, give any "
                "assistant rules.md, config.json and the digest below, and ask it to follow rules.md. "
                "Then run `python3 scan.py check` on what it writes before posting, since that "
                "assistant cannot run it.")
        parts = [marker, head]

    used = sum(len(part) + 2 for part in parts) + 1
    share = limit - used if drafts and drafts.strip() else (limit - used) * 2 // 5
    changes = render_within(digest, share - len(details % ("What changed", "")))
    if changes is not None:
        parts.append(details % ("What changed", changes.strip()))
        used += len(parts[-1]) + 2

    if not (drafts and drafts.strip()):
        wrapper = "```json\n%s\n```"
        room = limit - used - len(details % ("Digest for an assistant", wrapper % "")) - 2
        digest_json = for_assistant(digest, room)
        if digest_json is None:
            parts[1] += (" The digest was too large to include; run `python3 scan.py scan --json "
                         "--since %s` to get it." % digest["since"])
        else:
            parts.append(details % ("Digest for an assistant", wrapper % digest_json))

    body = "\n\n".join(parts) + "\n"
    if len(body) > limit:  # only reachable through the note above; never cut the marker
        body = body[:limit - len(TRUNCATED) - 1].rstrip() + TRUNCATED + "\n"
    return body


# ----------------------------------------------------------------- checking drafts

POST_BLOCK = re.compile(r"^```([a-z-]+)[^\n]*\n(.*?)\n```[ \t]*$", re.DOTALL | re.MULTILINE)
HASHTAG = re.compile(r"(?<![\w&#/])#(?=\w*[^\W\d])\w+")
ANNOTATION = re.compile(r"\n> (?:Over the limit: \d+ of \d+ characters\. Trim before posting\."
                        r"|\d+ hashtags?: the rules allow \d+\.)(?=\n|$)")
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
    text = ANNOTATION.sub("", text)  # a re-run replaces the old flags rather than stacking them

    def annotate(found):
        problems = block_problems(found.group(1), found.group(2).strip(), platforms)
        return "\n".join([found.group(0)] + ["> " + problem for problem in problems])

    return POST_BLOCK.sub(annotate, text)


# ----------------------------------------------------------------- self test

class FakeGitHub:
    """Canned answers keyed by path, so the tests never touch the network."""

    def __init__(self, pages, readmes=None):
        self.pages, self.readmes, self.calls = pages, readmes or {}, []

    def get(self, path, params=None, missing=None):
        self.calls.append(path)
        return self.pages.get(path, missing)

    def get_all(self, path, params=None):
        self.calls.append(path)
        return self.pages.get(path, [])

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

    def commit(sha, message, login="me", parents=1, name="Me", date="2026-09-20T09:00:00Z"):
        return {"sha": sha * 7, "html_url": "u", "parents": [{}] * parents,
                "author": {"login": login} if login else None,
                "commit": {"message": message, "author": {"name": name, "date": date}}}

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
        "/repos/me/rewritten/compare/rrrrrrr...main": {"status": "diverged", "commits": [
            commit(str(i), "old %d" % i, date="2026-01-01T00:00:00Z") for i in range(9)]},
        "/repos/me/rewritten/commits": [commit("k", "Rewrite trailers out")],
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
    check("a rewritten head falls back to dates",
          [c["subject"] for c in by_name["rewritten"]["commits"]], ["Rewrite trailers out"])
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
    check("the next window starts where this scan ended",
          resolve_since(config, [issue("2026-09-21T07:02:00Z", digest["state"])])[0],
          t("2026-09-21T07:00:00Z"))

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
        moment, reason = resolve_since(config, fetch_draft_issues(github, config, 1))
        print("%s  (%s)" % (format_time(moment), reason))
        return 0

    digest = build_digest(github, config, override=args.since)
    if args.out:
        Path(args.out).write_text(json.dumps(digest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(digest, indent=2) if args.json else render(digest), end="\n" if args.json else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
