#!/usr/bin/env python3
"""
Post drafts: the judgement half.

Hands rules.md, config.json and the digest from scan.py to Claude and prints the drafts
on stdout.

This is the only vendor specific file in the repository. Its contract is three lines long:

    read rules.md and config.json, read the digest, print the drafts

To move to a different provider, reimplement that contract in this file, then change the
secret and the package named in the workflow's "Write the drafts" step. Nothing else changes.

    pip install anthropic
    export ANTHROPIC_API_KEY=...
    python3 scan.py scan --json --out digest.json
    python3 draft.py digest.json                  print the drafts
    python3 draft.py digest.json --dry-run        print the prompt and exit, no API call, no key needed
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent

MODEL = "claude-opus-5"

SYSTEM = """You write social media post drafts for one person, from what changed in their public
GitHub repositories. Follow the rules below exactly. They are the whole specification.

The digest in the user message was produced by `python3 scan.py scan --json`, the scanner those
rules tell you to trust. It is the complete list of what is new. Do not add work that is not in
it, and do not second guess which commits are new.

Output only the drafts, in the format section 7 of the rules specifies, or `NOTHING TO POST`
as section 3 describes. No preamble, no closing summary, no offer to help further.

--- rules.md ---
%s
--- end rules.md ---

--- config.json ---
%s
--- end config.json ---
"""

USER = """Today is %s.

Digest from the scanner:

```json
%s
```

Write the drafts."""


def build_prompt(digest_path, today):
    rules = (HERE / "rules.md").read_text(encoding="utf-8")
    config = (HERE / "config.json").read_text(encoding="utf-8")
    digest = json.loads(Path(digest_path).read_text(encoding="utf-8"))
    # The state marker is bookkeeping for the next scan, not something to write about.
    digest.pop("state", None)
    return SYSTEM % (rules, config), USER % (today, json.dumps(digest, indent=2))


def ask_claude(system, user, no_fallbacks=False):
    import anthropic

    client = anthropic.Anthropic()
    request = {
        "model": MODEL,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    if not no_fallbacks:
        # A policy decline on the primary model is re-run on a fallback inside the same
        # call, so an unattended run does not come back empty. Drop it with
        # --no-fallbacks if this account does not have the beta.
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"

    with client.beta.messages.stream(**request) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise SystemExit("The model declined this request (%s). Nothing was written."
                         % getattr(details, "category", "no category given"))
    if message.stop_reason == "max_tokens":
        raise SystemExit("The drafts ran past max_tokens and were cut off. Nothing was written.")

    text = "\n".join(block.text for block in message.content if block.type == "text")
    if not text.strip():
        raise SystemExit("The model returned no text. stop_reason was %r." % message.stop_reason)
    return text.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("digest", help="the JSON digest written by scan.py scan --json --out")
    parser.add_argument("--today", help="date the drafts as this day instead of today")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt and exit without calling the API")
    parser.add_argument("--no-fallbacks", action="store_true",
                        help="do not send the server side refusal fallback parameters")
    args = parser.parse_args(argv)

    system, user = build_prompt(args.digest, args.today or date.today().isoformat())
    if args.dry_run:
        print(system)
        print(user)
        return 0

    print(ask_claude(system, user, no_fallbacks=args.no_fallbacks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
