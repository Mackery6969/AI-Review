# Paced AI Code Review

A GitHub Action that reviews a **whole repository** with Google Gemini, not just a pull request's changes. It sends the code a chunk at a time and waits out rate limits instead of failing. If it runs out of daily quota or time, it saves its progress and picks up where it left off on the next run, so it works within Gemini's free tier.

Findings are posted as comments on a tracking issue, one comment per chunk that has problems, each linked to the exact line.

## Usage

```yaml
name: Full AI Review

on:
  schedule:
    - cron: "30 8 * * *" # shortly after Gemini's daily quota resets (midnight Pacific)
  workflow_dispatch:

permissions:
  contents: read
  issues: write

concurrency:
  group: full-ai-review

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v7

      - uses: Mackery6969/AI-Review@v1
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          include: src/**/*.java
          instructions: A Minecraft mod for NeoForge 1.21.1.
```

Add your Gemini API key (from Google AI Studio) as a repository secret named `GEMINI_API_KEY`.

## How a review runs

1. The first run records the current commit and opens an issue labelled `ai-review`. Every later run reviews that same commit, so pushes during a review don't change what it's reviewing.
2. Matching files are sorted by path and packed into chunks of about `chunk-lines` lines. Each chunk is one Gemini request.
3. When Gemini says "too many requests", the action waits as long as Gemini asks and retries. When the daily quota runs out, or the time budget is used up, the action stops cleanly. The next run continues from the next unreviewed chunk.
4. When every chunk is done, the issue is marked complete. Later runs do nothing until you **close the issue**. The run after that starts a fresh review of the latest commit.

If a chunk fails for a reason that won't fix itself, such as a blocked or unreadable response, it's marked as failed with a comment explaining why. The review then moves on.

Run with `dry-run: "true"` to see how your files would be chunked without calling any API.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `gemini-api-key` | — | Gemini API key. Required. |
| `github-token` | `github.token` | Token used to manage the tracking issue. Needs `issues: write`. |
| `model` | `gemini-2.5-flash` | Gemini model to review with. |
| `include` | `**/*.java` | Globs of files to review, separated by commas or newlines. |
| `exclude` | | Globs of files to skip. |
| `instructions` | | Context for the reviewer: what the project is and what to watch for. |
| `chunk-lines` | `3000` | About how many lines of code go in one request. |
| `min-severity` | `medium` | Lowest severity to report: `high`, `medium`, or `low`. |
| `requests-per-minute` | `5` | Most requests to send per minute. Keep it under your model's per-minute limit. |
| `time-budget-minutes` | `330` | Stop and save progress after this long. Keep it below the job's `timeout-minutes`. |
| `dry-run` | `false` | Only print the chunk plan. Needs no secrets. |

## Limitations

- Each chunk is reviewed on its own, so bugs that only appear when you follow code across chunks are mostly missed.
- Only Gemini is supported.
- The review's progress is stored in a hidden comment in the issue body. Editing the issue body by hand can lose it.
