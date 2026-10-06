# Evaluate your reviewer offline

You can score any code reviewer on the benchmark from your own machine. You do
not need to register, open a pull request, or publish anything. It takes two
steps:

1. Run your reviewer on the benchmark pull requests to produce findings files.
2. Judge those files against the golden set to get metrics.

Offline results are for development, comparison, and tuning. Only a final run
started from the [self-service portal](https://review-bench.ai/submit) can
appear on the leaderboard.

```sh
git clone https://github.com/review-bench/ReviewBench && cd ReviewBench
npm ci

# 1. Findings for the 25 test set pull requests land in ./findings/
scripts/try-agent.sh --command /path/to/my-reviewer.sh -e RB_AGENT=my-reviewer

# 2. Judge them
npm run judge -- --candidate ./findings --provider <provider> --model <model-id> \
  --output ./scoring/my-reviewer.json --concurrency 4
```

## 1. Produce findings

[`scripts/try-agent.sh`](../scripts/try-agent.sh) handles each pull request
the same way. It fetches the base and head commits from the
[review-bench mirror](https://github.com/review-bench), checks out the head,
writes the diff and pull request metadata, and runs your reviewer. It then
validates the findings file and copies it to `./findings/<pr key>.json`. Add
`--set full` to run all 219 pull requests, or `--pr <index>` to run one.

It needs git and jq. It also needs docker if you run an image. The script
exits non-zero if any pull request fails.

### Option A: a container image

```sh
scripts/try-agent.sh my-reviewer:dev -e OPENAI_API_KEY
```

The image must follow the [agent contract](../AGENT_CONTRACT.md). Use this
option if you plan to submit to the leaderboard later. It is the same image
you would register.

### Option B: a command on your machine

```sh
scripts/try-agent.sh --command ./my-reviewer.sh -e RB_AGENT=my-reviewer
```

`--command` runs a shell command on the host instead of in a container. Use it
for a reviewer that already runs on your machine, such as a CLI, an internal
service client, or a script that calls your API. The command runs from the
checkout at head and receives the same variables as a container. Paths point to
host directories:

| Variable | Contents |
|---|---|
| `RB_REPO` | The checkout at `RB_HEAD`, also the working directory. Writable; it is reset before the next pull request. |
| `RB_DIFF` | The three-dot diff from base to head |
| `RB_PR_JSON` | `{ repo, pr_number, base, head, nwo, title, body }` |
| `RB_OUT` | Where to write the findings file |
| `RB_NWO`, `RB_PR_NUMBER`, `RB_BASE`, `RB_HEAD` | Pull request identity |
| `RB_AGENT` | `try-agent` unless you pass `-e RB_AGENT=<name>` |

Variables in your shell are already visible to the command. `-e NAME=VALUE`
sets or overrides one for each run.

The findings file uses the format in the [agent contract](../AGENT_CONTRACT.md#what-you-must-write).
A minimal adapter converts your reviewer's output:

```sh
#!/usr/bin/env bash
set -euo pipefail
my-reviewer review --base "$RB_BASE" --head "$RB_HEAD" --json > /tmp/raw.json
jq --slurpfile pr "$RB_PR_JSON" '{
  pr: ($pr[0] | {repo, pr_number, base, head}),
  agent: env.RB_AGENT,
  findings: [.comments[] | {file: .path, start_line: .start, end_line: .end, message: .body, producer: env.RB_AGENT}]
}' /tmp/raw.json > "$RB_OUT"
```

`try-agent.sh` records each pull request's wall-clock time as
`usage.time_in_ms` unless your findings file sets it. The judge reports this
value as review duration.

To run several pull requests at once, start one `try-agent.sh --pr <index>` per
worker from the same directory. Two workers can share a repository in the full
set, so give each worker its own `TRY_AGENT_WORK` directory.

### Already have findings?

If your own harness produces findings, write them in the
[judging input format](JUDGING_INPUT.md) and skip to step 2.

## 2. Judge the findings

```sh
export ANTHROPIC_API_KEY=...
npm run judge -- --candidate ./findings --provider anthropic --model <model-id> \
  --output ./scoring/my-reviewer.json --concurrency 4
```

The judge matches each finding against the golden set. It classifies the
findings that match nothing, then writes grounded and augmented precision and
recall to `scoring/my-reviewer.json`. Per-finding decisions go to
`scoring/my-reviewer.details.json`. See [How to judge findings](JUDGING.md) for
providers, resuming, and the output fields.

### Use the leaderboard's judge model

The leaderboard is judged by Claude Sonnet 5. The judge model changes the
scores, so use the same model when you compare with the leaderboard. If the
bundled model registry does not list it yet (`npx pi --list-models`), add it to
a `models.json`. You can also route the judge through a proxy or an
Anthropic-compatible gateway the same way:

```sh
mkdir -p .judge
cat > .judge/models.json <<'EOF'
{
  "providers": {
    "anthropic": {
      "apiKey": "ANTHROPIC_API_KEY",
      "models": [
        { "id": "claude-sonnet-5", "reasoning": true, "contextWindow": 1000000, "maxTokens": 64000 }
      ]
    }
  }
}
EOF
PI_CODING_AGENT_DIR=$PWD/.judge npm run judge -- --candidate ./findings \
  --provider anthropic --model claude-sonnet-5 --output ./scoring/my-reviewer.json
```

For a gateway, add `"baseUrl"` to the provider. Set `"apiKey"` to the name of
the environment variable that holds its key. `PI_CODING_AGENT_DIR` keeps this
configuration out of your personal pi settings. The
[pi models documentation](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md)
lists every option.

## Compare results fairly

Offline numbers can differ from leaderboard numbers for reasons unrelated to
reviewer quality:

- **Set size.** The 25 test set pull requests show whether your adapter works.
  The leaderboard scores all 219 pull requests three times. Use `--set full`
  before comparing.
- **Coverage.** The judge scores only the pull requests that have a findings
  file. A failed pull request drops out instead of counting as a miss. Check
  that `try-agent.sh` reports `failed 0` and that the judge summary shows the
  expected pull request count.
- **Judge.** Record the provider and model with every result. The results file
  stores both, along with the prompt and golden set hashes.
- **Environment.** `try-agent.sh` does not enforce the time limit or the
  network allowlist. It uses full-history fetches, not the benchmark's
  minimised repositories.
- **Answer leakage.** All 219 pull requests are public, and the golden findings
  are in this repository. The upstream pull request's review comments, which
  are a source of golden findings, are public too, along with later fix
  commits. A reviewer with network access can look up the answers instead of
  finding the issues. Reviewers built to read pull request discussions will do
  this unprompted. One fetched `pulls/<n>/comments` from the unauthenticated
  GitHub API in our testing, so withholding a GitHub token is not enough.
  `try-agent.sh` does not restrict the network for either option. Do what the
  benchmark does: allow outbound traffic only to your model provider. Also keep
  `golden/` unreadable and remove the checkout's remotes. Then check the
  reviewer's logs for requests to GitHub.
