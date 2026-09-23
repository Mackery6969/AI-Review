# Paced AI Code Review

A GitHub Action that reviews your **entire repository** with Google Gemini, not just the changes in a pull request.

Most AI review actions look at a pull request's diff, so code that was already in the repo is never looked at. This action works through the whole codebase a chunk at a time and reports real defects: bugs, crashes, leaks, concurrency problems, and security issues. It's built to run on Gemini's free tier:

- **It waits instead of failing.** When Gemini says you're sending requests too fast, the action waits as long as Gemini asks and carries on.
- **It resumes where it left off.** When the daily quota runs out, or the job is close to its time limit, it saves its progress and stops cleanly. The next run continues from the next unreviewed chunk, so a large codebase is finished over several days.
- **Findings end up in one place.** Everything is reported on a single tracking issue, with each finding linked to the exact line.

## Contents

- [Quick start](#quick-start)
- [What you get](#what-you-get)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Examples](#examples)
- [Choosing a model and staying within quota](#choosing-a-model-and-staying-within-quota)
- [Privacy and security](#privacy-and-security)
- [Versioning](#versioning)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [License](#license)

## Quick start

**1. Get a Gemini API key.** Create one in [Google AI Studio](https://aistudio.google.com/apikey). You don't need a billing account for the free tier.

**2. Add the key to your repository.** Go to **Settings → Secrets and variables → Actions → New repository secret**, name it `GEMINI_API_KEY`, and paste in the key.

**3. Add a workflow.** Create `.github/workflows/full-ai-review.yml`:

```yaml
name: Full AI Review

on:
  schedule:
    - cron: "30 8 * * *" # daily, shortly after Gemini's quota resets at midnight Pacific
  workflow_dispatch: # also lets you start it by hand from the Actions tab

permissions:
  contents: read
  issues: write

concurrency:
  group: full-ai-review # never run two reviews at once

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
          instructions: A Java library for parsing configuration files.
```

**4. Start it.** Go to **Actions → Full AI Review → Run workflow**, or wait for the schedule. A new issue labelled `ai-review` appears and fills up as the review progresses.

To see how your code will be split before spending any quota, run once with `dry-run: "true"`. See [Examples](#examples).

## What you get

The first run opens a tracking issue titled **AI code review of `abc1234`**. The issue body shows:

- which commit is being reviewed and with which model,
- how many chunks are done and how many findings have been reported so far,
- a checklist of every chunk, ticked off as each one is reviewed.

Each chunk with problems gets its own comment on the issue, sorted from most to least severe. In the real comments, each file location links to that line at the reviewed commit:

> ### `src/main/java/com/example/net/` Client.java … Server.java — 2 findings
>
> **High** · `src/main/java/com/example/net/Server.java:88` — Socket is never closed when the handshake fails
>
> The early `return` on line 88 skips the `close()` call on line 97, so every failed handshake leaks a file descriptor.
>
> **Medium** · `src/main/java/com/example/net/Client.java:41` — `retries` is decremented twice per loop
>
> Both the loop header and the `catch` block decrement it, so the client gives up after half the configured attempts.

Chunks with no findings don't get a comment. They're just ticked off in the checklist.

The job's run summary, on the workflow run page, says how many chunks were reviewed in that run and why it stopped.

## How it works

1. **Pin a commit.** On the first run the action records the current commit, and every later run reviews that same commit. Pushes made during a review don't change what's being reviewed, and line links stay accurate.
2. **Pick files.** It lists every file in that commit that matches `include` and doesn't match `exclude`. Binary files and symlinks are skipped.
3. **Split into chunks.** Files are sorted by path and packed into chunks of about `chunk-lines` lines, so files from the same folder usually end up together. A single file bigger than `chunk-lines` becomes its own chunk. It is not split.
4. **Review each chunk.** Each chunk is one request to Gemini, with line numbers added and your `instructions` included. Gemini is asked for defects only, not style, and must reply in a fixed structure so the action can link each finding to its line. Findings below `min-severity` are dropped.
5. **Save progress after every chunk.** Progress is stored in a hidden comment at the top of the issue body. There are no extra files, branches, or caches.
6. **Stop cleanly.** The run ends when every chunk is done, when Gemini's daily quota is used up, or when `time-budget-minutes` has passed. The next run finds the open issue and continues.
7. **Finish, then start over when you choose.** Once every chunk is reviewed, the issue says **Complete** and later runs do nothing. **Close the issue** when you want a new review. The next run starts a fresh one on the latest commit.

### Rate limits and errors

| What happens | What the action does |
| --- | --- |
| Too many requests per minute | Waits as long as Gemini asks (or backs off if it doesn't say), then retries the same chunk. Keeps retrying until the time budget runs out. |
| Daily quota used up | Stops the run. The chunk is retried on the next run. |
| Gemini server or network error | Backs off and retries, up to 5 times, then marks the chunk as failed. |
| Response is blocked, cut off, or unreadable | Marks the chunk as failed and posts a comment with the reason. |
| Invalid API key or unknown model | Fails the job immediately with an error, since retrying won't help. |

Failed chunks are not retried automatically. To retry them, close the issue and start a new review.

## Configuration

| Input | Default | Description |
| --- | --- | --- |
| `gemini-api-key` | *(required)* | Your Gemini API key. Pass it from a secret. |
| `github-token` | `${{ github.token }}` | Token used to create and update the tracking issue. It needs `issues: write`. |
| `model` | `gemini-2.5-flash` | Which Gemini model reviews the code. |
| `include` | `**/*.java` | Files to review, as glob patterns separated by commas or newlines. |
| `exclude` | *(none)* | Files to skip, in the same format. Checked after `include`. |
| `instructions` | *(none)* | Context about your project, added to every request. See below. |
| `chunk-lines` | `3000` | About how many lines of code go in each request. |
| `min-severity` | `medium` | Lowest severity to report: `high`, `medium`, or `low`. |
| `requests-per-minute` | `5` | Most requests to send per minute. Set it at or below your model's per-minute limit. |
| `time-budget-minutes` | `330` | Stop and save progress after this many minutes. Keep it below the job's `timeout-minutes`, or the job may be killed before it saves. |
| `dry-run` | `false` | Print the chunk plan and exit without calling Gemini or GitHub. It needs no secrets. |

### Glob patterns

Patterns are matched against paths from the repository root.

- `*` matches anything within one folder name.
- `**` matches any number of folders.
- `?` matches a single character.

| Pattern | Matches |
| --- | --- |
| `**/*.java` | Every `.java` file anywhere |
| `src/**/*.ts` | `.ts` files anywhere under `src/` |
| `src/*.py` | `.py` files directly in `src/`, not in subfolders |
| `**/generated/**` | Everything inside any folder named `generated` |

### Writing good `instructions`

Gemini only sees one chunk at a time, so a sentence or two of context helps it tell a real bug from an intentional choice. For example:

- what the project is and what it runs on, e.g. "A Minecraft mod for NeoForge 1.21.1, recently ported from Forge 1.20.1",
- the kind of problem you most care about, e.g. "Watch for code that runs on the wrong thread",
- conventions that could look wrong, e.g. "Methods ending in `Unchecked` skip validation on purpose".

## Examples

### See the chunk plan before spending quota

```yaml
      - uses: Mackery6969/AI-Review@v1
        with:
          gemini-api-key: unused
          include: src/**/*.java
          dry-run: "true"
```

The log lists every chunk with its file and line counts, and ends with the total number of requests a full review will make.

### Several languages, skipping generated and test code

```yaml
      - uses: Mackery6969/AI-Review@v1
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          include: |
            src/**/*.ts
            src/**/*.tsx
            scripts/**/*.py
          exclude: |
            **/generated/**
            **/*.test.ts
```

### Only the serious problems, with a stronger model

```yaml
      - uses: Mackery6969/AI-Review@v1
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          model: gemini-2.5-pro
          min-severity: high
          requests-per-minute: "2"
```

### Run by hand only

Leave out the `schedule` trigger and keep only `workflow_dispatch`. Re-run the workflow each time you want it to continue a review that stopped on quota.

## Choosing a model and staying within quota

Gemini's free tier limits both requests per minute and requests per day, and the limits differ by model and change over time. Check [Google's rate limit page](https://ai.google.dev/gemini-api/docs/rate-limits) for the current numbers.

- **Estimate the size first.** A full review makes one request per chunk. Run a dry run to see the exact count. For example, about 70,000 lines at the default `chunk-lines` comes to around 26 requests.
- **Flash or Pro.** `gemini-2.5-flash` has more generous free limits and is the default. A Pro model usually gives more careful reviews but allows far fewer requests per day, so a large codebase takes more days to finish.
- **Chunk size.** Larger chunks mean fewer requests and more context per review, but each request is bigger and counts more against your tokens-per-minute limit. The default of 3,000 lines is a middle ground.
- **Pacing.** Setting `requests-per-minute` at or below your model's limit avoids most rate-limit waits. If you set it higher, the action still works, but it spends time waiting.

## Privacy and security

- **Your code is sent to Google.** Every reviewed file goes to the Gemini API. Google's terms for the free tier allow it to use what you send to improve its products. Read the current [Gemini API terms](https://ai.google.dev/gemini-api/terms) before using this on private or proprietary code, and consider a paid tier if that matters.
- **Minimal permissions.** The action needs `contents: read` to check out the code and `issues: write` to manage the tracking issue. It doesn't push code, open pull requests, or change settings.
- **Keep the key in a secret.** Never write the API key into the workflow file. GitHub doesn't pass secrets to workflows triggered by pull requests from forks or by Dependabot, which is another reason to run this on a schedule rather than on pull requests.
- **Pin the version you trust.** `@v1` follows the latest 1.x release. To lock to exact code, pin a commit SHA instead, e.g. `Mackery6969/AI-Review@<full-commit-sha>`. Dependabot can keep either form up to date.
- **Model output is treated as untrusted.** The code being reviewed could contain text meant to steer the model, so the action treats every finding as untrusted. It's posted as plain comment text, and @-mentions and issue references are defused so a finding can't notify people or link to other issues. The worst a manipulated review can do is post misleading comments on the tracking issue.
- **Reporting vulnerabilities.** See [SECURITY.md](SECURITY.md). Please report privately, not in a public issue.

## Versioning

Releases follow [semantic versioning](https://semver.org). Each release is tagged `vMAJOR.MINOR.PATCH`, and the major tag (`v1`) is moved to the newest release in that series. Using `@v1` gets you fixes and new features without breaking changes. Breaking changes, such as renamed inputs or a different issue format, only come with a new major version.

## Troubleshooting

**"gemini-api-key is empty"**
The secret is missing, misnamed, or unavailable to this run. Check the secret name. Runs triggered from forks or by Dependabot never receive secrets.

**"Gemini rejected the request (401/403/404)"**
The API key is invalid or restricted, or the `model` name is wrong or has been retired. Check the model name against Google's current model list.

**The job keeps stopping with "Gemini's daily quota ran out"**
This is expected on the free tier for large codebases. The review continues on the next scheduled run. To finish sooner, use a model with a higher daily limit, raise `chunk-lines` to make fewer requests, or enable billing on your Google project.

**Every run says the review is complete and does nothing**
The previous review finished. Close the tracking issue to start a new one.

**A chunk says "review failed"**
Its comment explains why. A cut-off or unreadable response usually means the chunk was too large, so lower `chunk-lines`. A blocked response means Gemini refused the content. Failed chunks are retried only in a new review.

**No issue appears**
Make sure the workflow grants `issues: write` and that issues are enabled for the repository (**Settings → General → Features**).

**The progress looks wrong after I edited the issue**
Progress is stored in a hidden comment at the top of the issue body. If it's removed or damaged, the action no longer recognizes the issue. Close the issue and start a new review.

## Limitations

- **Each chunk is reviewed on its own.** Bugs that only show up when you follow code across chunks, such as a caller in one folder misusing a method defined in another, are mostly missed.
- **Gemini only.** Other providers aren't supported yet.
- **AI reviews make mistakes.** Some findings will be wrong, and some real bugs will be missed. Treat findings as leads to check, not verdicts. This action complements tests and static analysis; it doesn't replace them.
- **One review at a time per repository.** Only one tracking issue is active at once.

## License

[MIT](LICENSE)
