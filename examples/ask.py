"""Send one question to a Claude model and print the answer.

A minimal client for testing cost-per-task without Claude Code's large system
prompt, so a simple question costs a fraction of a cent. Standard library
only. Reads ANTHROPIC_API_KEY, and ANTHROPIC_BASE_URL when set (cpt run sets
it to the proxy).

    python examples/ask.py --model claude-fable-5-1 "What is 17 * 23? Reply with only the number."

With --expect, the exit code says whether the answer contained the expected
text (0 yes, 1 no), so a script can label the attempt automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask a Claude model one question.")
    parser.add_argument("prompt")
    parser.add_argument("--model", default="claude-fable-5-1")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=8000,
        help="Fable models always think first, and thinking counts toward this cap",
    )
    parser.add_argument("--expect", help="text the answer must contain to count as a pass")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ask.py: set ANTHROPIC_API_KEY first", file=sys.stderr)
        return 2
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    body = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": args.prompt}],
    }
    request = urllib.request.Request(
        base_url + "/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"ask.py: API error {exc.code}: {detail}", file=sys.stderr)
        return 3

    if data.get("stop_reason") == "max_tokens":
        print("ask.py: warning: the answer hit --max-tokens and may be cut off", file=sys.stderr)
    answer = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    print(answer)

    if args.expect is not None:
        # Ignore spacing and case, so "apple,banana" matches "apple, banana".
        def squash(text: str) -> str:
            return "".join(text.split()).lower()

        passed = squash(args.expect) in squash(answer)
        print(f"CHECK: {'pass' if passed else 'fail'} (expected '{args.expect}')", file=sys.stderr)
        return 0 if passed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
