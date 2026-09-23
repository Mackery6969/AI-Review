# Paced AI Code Review

A GitHub Action that reviews your **whole repository** with Google Gemini once, then keeps it reviewed by checking only the files that change. Findings show up as **code scanning alerts**, next to CodeQL's, under an "AI Review" tool.

Most AI review actions only look at a pull request's diff, so code that was already in the repository is never checked. This action starts with a review of the entire codebase. After that, each run reviews only the files changed since the last review, so nothing is read twice. It reports real defects: bugs, crashes, leaks, concurrency problems, and security issues.

It's built to run on Gemini's free tier:

- **It never reviews the same code twice.** After the first full review, only changed files are sent. If nothing changed since the last review, the run stops straight away. Once a month, by default, it forgets and reviews everything again.
- **It waits instead of failing.** When Gemini says you're sending requests too fast, the action waits as long as Gemini asks and carries on.
- **It resumes where it left off.** When the daily quota runs out, or the job is close to its time limit, it saves its progress and stops cleanly. The next run continues from the next unreviewed chunk, so even a large first review gets finished over a few days.
- **Findings go where your other code findings are.** Each one becomes an alert in **Security → Code scanning**, pinned to the exact line. Alerts disappear on their own when the code they point at changes and the problem is gone.

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

**Requirements:** code scanning has to be available for the repository. It is for every public repository. Private repositories need GitHub Code Security (part of GitHub Advanced Security).

**1. Get a Gemini API key.** Create one in [Google AI Studio](https://aistudio.google.com/apikey). You don't need a billing account for the free tier.

**2. Add the key to your repository.** Go to **Settings → Secrets and variables → Actions → New repository secret**, name it `GEMINI_API_KEY`, and paste in the key.

**3. Add a workflow.** Create `.github/workflows/ai-review.yml`:

```yaml
name: AI Review

on:
  push: # review what each push changes
  schedule:
    - cron: "30 8 * * *" # daily, to resume a review that stopped on quota
  workflow_dispatch: # also lets you start it by hand from the Actions tab
    inputs:
      full-review:
        description: Forget earlier reviews and review the whole project again
        type: boolean
        default: false

permissions:
  contents: read
  security-events: write

concurrency:
  group: ai-review # one review at a time; later runs wait instead of being cancelled
  cancel-in-progress: false

jobs:
  review:
    # Only review the default branch, so feature branches don't move the review forward.
    if: github.ref_name == github.event.repository.default_branch
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v7

      - uses: Mackery6969/AI-Review@v2
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          include: src/**/*.java
          instructions: A Java library for parsing configuration files.
          full-review: ${{ inputs.full-review || false }}
```

**4. Start it.** Push to your default branch, go to **Actions → AI Review → Run workflow**, or wait for the schedule. When the first full review finishes, its findings appear under **Security → Code scanning**. Filter by the tool **AI Review** to see only these. After that, each push is reviewed on its own.

To see how your code will be split before spending any quota, run once with `dry-run: "true"`. See [Examples](#examples).

## What you get

**Code scanning alerts.** Each finding is an alert with:

- a title describing the problem,
- the file and line it points at, shown inline on the code,
- a severity: **Error** for high, **Warning** for medium, **Note** for low,
- an explanation of what goes wrong and when.

The alerts carry no security severity, so they don't count as security vulnerabilities. You can dismiss an alert that's wrong, as you would a CodeQL alert. When a file changes, its alerts are replaced by the result of reviewing the new version. If the problem is gone, GitHub marks the alert as fixed.

**A run summary.** Every run's summary page (open the workflow run in the Actions tab) says what was reviewed and why the run stopped. It also has a table of the findings made in that run, each linked to its line:

> Finished reviewing changes from 1a2b3c4 to 4f2a9c1: 2 new findings this run.
>
> | Severity | Location | Finding |
> | --- | --- | --- |
> | High | `src/main/java/com/example/net/Server.java:88` | Socket is never closed when the handshake fails |
> | Medium | `src/main/java/com/example/net/Client.java:41` | `retries` is decremented twice per loop |

## How it works

The action remembers the last commit it finished reviewing. Every run does one of four things:

| Situation | What the run does |
| --- | --- |
| Nothing has been reviewed yet | Reviews **the whole project** at the current commit. |
| A review is still in progress | **Continues it** from the next unreviewed chunk. |
| New commits since the last review | Reviews **only the files that changed** between the last reviewed commit and the current one. |
| No new commits | **Stops straight away.** Nothing is sent to Gemini or uploaded. |

If new commits only touch files outside `include`, the run records them as reviewed without calling Gemini or uploading anything.

Every `reset-after-days` days (30 by default), the action forgets what it has seen and reviews the whole project again. This catches problems that only become visible over time, such as code that was fine until something it depends on changed. It also means an improved model gets to look at the whole codebase. To start a full review early, run the workflow by hand with **full-review** ticked.

Each review works like this:

1. **Pin a commit.** A review records the commit it started on, and every run that continues it reviews that same commit. Pushes made during a review don't change what's being reviewed, and line numbers stay accurate. Those pushes are picked up by the next review.
2. **Pick files.** A full review takes every file in that commit that matches `include` and doesn't match `exclude`. A review of changes takes only files that were added or modified since the last review, filtered the same way. Deleted files, binary files, and symlinks are skipped.
3. **Split into chunks.** Files are sorted by path and packed into chunks of about `chunk-lines` lines, so files from the same folder usually end up together. A single file bigger than `chunk-lines` becomes its own chunk. It is not split.
4. **Review each chunk.** Each chunk is one request to Gemini, with line numbers added and your `instructions` included. A changed file is sent whole, not just the edited lines, so Gemini sees the change in context. Gemini is asked for defects only, not style, and must reply in a fixed structure so each finding can be pinned to its line. Findings below `min-severity` are dropped.
5. **Save progress after every chunk.** Progress and findings are kept in the Actions cache, restored at the start of each run and saved at the end.
6. **Upload when the review finishes.** Code scanning closes any alert that's missing from the latest upload, so each upload contains every current finding. That means new findings for the files just reviewed, plus the kept findings for every file that hasn't changed. Findings for deleted files are dropped. If the findings didn't change, nothing is uploaded.

If the cache is ever lost, for example after a long period with no runs, the next run simply starts over with a full review.

### Rate limits and errors

| What happens | What the action does |
| --- | --- |
| Too many requests per minute | Waits as long as Gemini asks (or backs off if it doesn't say), then retries the same chunk. Keeps retrying until the time budget runs out. |
| Daily quota used up | Stops the run. The chunk is retried on the next run. |
| Gemini overloaded, server error, or network error | Backs off and retries, up to 5 times over about 15 minutes. If Gemini still isn't answering, it stops the run and the chunk is retried on the next run. An outage never counts against your code. |
| Response is blocked, cut off, or unreadable | Marks the chunk as failed with a warning in the run log. |
| Invalid API key or unknown model | Fails the job immediately with an error, since retrying won't help. Progress made so far is kept. |
| Upload to code scanning fails | Fails the job with an error. The next run retries the upload before doing anything else. |
| The last reviewed commit no longer exists, for example after a force-push | Logs a warning and reviews the whole project instead. |

Failed chunks aren't retried within the same review. A failed file is reviewed again the next time it changes, or at the next full review.

## Configuration

| Input | Default | Description |
| --- | --- | --- |
| `gemini-api-key` | *(required)* | Your Gemini API key. Pass it from a secret. |
| `github-token` | `${{ github.token }}` | Token used to upload findings to code scanning. It needs `security-events: write`. |
| `model` | `gemini-3.8-flash` | Which Gemini model reviews the code. See [Google's model list](https://ai.google.dev/gemini-api/docs/models) for current names. |
| `include` | `**/*.java` | Files to review, as glob patterns separated by commas or newlines. |
| `exclude` | *(none)* | Files to skip, in the same format. Checked after `include`. |
| `instructions` | *(none)* | Context about your project, added to every request. See below. |
| `chunk-lines` | `3000` | About how many lines of code go in each request. |
| `min-severity` | `medium` | Lowest severity to report: `high`, `medium`, or `low`. |
| `requests-per-minute` | `5` | Most requests to send per minute. Set it at or below your model's per-minute limit. |
| `reset-after-days` | `30` | Days after a full review before the whole project is reviewed again. `0` never resets. |
| `full-review` | `false` | Set to `true` to forget earlier reviews and start a full review now, discarding any review in progress. |
| `time-budget-minutes` | `330` | Stop and save progress after this many minutes. Keep it below the job's `timeout-minutes`, or the job may be killed before it saves. |
| `dry-run` | `false` | Print how the whole project would be chunked, then exit without calling Gemini or GitHub. It needs no secrets. |

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
      - uses: Mackery6969/AI-Review@v2
        with:
          gemini-api-key: unused
          include: src/**/*.java
          dry-run: "true"
```

The log lists every chunk of a full review with its file and line counts, and ends with the total number of requests it will make.

### Skip runs for pushes that don't touch reviewed files

The action already stops quickly when no matching file changed, but a `paths` filter saves starting a runner at all. Keep it in step with `include`:

```yaml
on:
  push:
    paths:
      - "src/**/*.java"
```

Skipped pushes aren't lost. The next review covers everything since the last reviewed commit.

### Several languages, skipping generated and test code

```yaml
      - uses: Mackery6969/AI-Review@v2
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
      - uses: Mackery6969/AI-Review@v2
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          model: gemini-3.1-pro-preview
          min-severity: high
          requests-per-minute: "2"
```

### Never forget what's been reviewed

```yaml
      - uses: Mackery6969/AI-Review@v2
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          reset-after-days: "0"
```

A full review then only happens when you ask for one with `full-review`.

## Choosing a model and staying within quota

Gemini's free tier limits both requests per minute and requests per day, and the limits differ by model and change over time. Your exact limits for each model are shown on the [AI Studio rate limit page](https://aistudio.google.com/rate-limit).

- **The full review is the expensive part.** It makes one request per chunk. Run a dry run to see the exact count. For example, about 70,000 lines at the default `chunk-lines` comes to around 26 requests. Reviews of changes are usually just one or two requests.
- **Flash or Pro.** `gemini-3.8-flash` is the default. A Pro model usually gives more careful reviews but allows far fewer requests per day, especially while it's a preview, so a large first review takes more days to finish.
- **Retired models.** Google stops offering older models to new API keys over time. If a run fails with a 404 saying a model is "no longer available", set `model` to a current one.
- **Chunk size.** Larger chunks mean fewer requests and more context per review, but each request is bigger and counts more against your tokens-per-minute limit. The default of 3,000 lines is a middle ground.
- **Pacing.** Setting `requests-per-minute` at or below your model's limit avoids most rate-limit waits. If you set it higher, the action still works, but it spends time waiting.
- **Reset interval.** Each reset repeats the full review. Raise `reset-after-days`, or set it to `0`, to spend less quota.

## Privacy and security

- **Your code is sent to Google.** Every reviewed file goes to the Gemini API. Google's terms for the free tier allow it to use what you send to improve its products. Read the current [Gemini API terms](https://ai.google.dev/gemini-api/terms) before using this on private or proprietary code, and consider a paid tier if that matters.
- **Minimal permissions.** The action needs `contents: read` to check out the code and `security-events: write` to upload alerts. It doesn't push code, open issues or pull requests, or change settings. Progress is kept in the Actions cache, which needs no extra permission.
- **Keep the key in a secret.** Never write the API key into the workflow file. GitHub doesn't pass secrets to workflows triggered by pull requests from forks or by Dependabot, which is why the example runs on pushes to the default branch rather than on pull requests.
- **Pin the version you trust.** `@v2` follows the latest 2.x release. To lock to exact code, pin a commit SHA instead, e.g. `Mackery6969/AI-Review@<full-commit-sha>`. Dependabot can keep either form up to date.
- **Model output is treated as untrusted.** The code being reviewed could contain text meant to steer the model. Findings are only ever uploaded as plain-text code scanning results. They can't run anything, notify anyone, or change your repository. The worst a manipulated review can do is create misleading alerts, which you can dismiss.
- **Saved progress is checked before use.** Everything restored from the cache is validated before any of it reaches `git`. Anything unexpected is thrown away and the action starts a fresh full review.
- **Reporting vulnerabilities.** See [SECURITY.md](SECURITY.md). Please report privately, not in a public issue.

## Versioning

Releases follow [semantic versioning](https://semver.org). Each release is tagged `vMAJOR.MINOR.PATCH`, and the major tag (such as `v2`) is moved to the newest release in that series. Using `@v2` gets you fixes and new features without breaking changes. Breaking changes, such as renamed inputs or different required permissions, only come with a new major version.

**Upgrading from v1:** v1 reported findings on a tracking issue. v2 reports them to code scanning instead. In your workflow, replace the `issues: write` permission with `security-events: write` and change `@v1` to `@v2`. The first v2 run does a full review. You can close the old tracking issue and delete the `ai-review` label.

## Troubleshooting

**"gemini-api-key is empty"**
The secret is missing, misnamed, or unavailable to this run. Check the secret name. Runs triggered from forks or by Dependabot never receive secrets.

**"Gemini rejected the request (401/403/404)"**
The API key is invalid or restricted, or the `model` name is wrong or has been retired. Check the model name against Google's current model list.

**"GitHub refused the code scanning upload"**
Make sure the workflow grants `security-events: write`. For a private repository, code scanning must be enabled, which needs GitHub Code Security. The findings are kept, and the next run retries the upload.

**The job keeps stopping with "Gemini's daily quota ran out"**
This is expected during the first full review of a large codebase. The review continues on the next scheduled run. To finish sooner, use a model with a higher daily limit, raise `chunk-lines` to make fewer requests, or enable billing on your Google project.

**No alerts appear**
Alerts are uploaded only when a review finishes. Check the latest run's summary: if it says the review stopped on quota or time, the alerts arrive when a later run finishes it. In **Security → Code scanning**, filter by the tool **AI Review**.

**A run says "Nothing new to review"**
Nothing has been committed since the last finished review, so there's nothing to do. Run the workflow with **full-review** ticked if you want a full review now.

**A push wasn't reviewed**
Check that the push was to the default branch. If a review was already in progress, the push is covered by the next review, once the current one finishes.

**A warning says a chunk's review failed**
The warning gives the reason. A cut-off or unreadable response usually means the chunk was too large, so lower `chunk-lines`. A blocked response means Gemini refused the content.

**"The saved review state is unreadable"**
The cached progress was damaged or came from an incompatible version. The action discards it and starts a full review. Nothing else needs doing.

## Limitations

- **Each chunk is reviewed on its own.** Bugs that only show up when you follow code across chunks, such as a caller in one folder misusing a method defined in another, are mostly missed.
- **Unchanged files aren't re-checked until the next reset.** If a change in one file breaks another file that didn't change, the broken file isn't looked at again until the next full review.
- **An alert may be re-created with new wording.** When a file changes, it's reviewed again from scratch. If Gemini reports a problem it had already found in different words, the old alert is closed and a new one is opened.
- **Gemini only.** Other providers aren't supported yet.
- **AI reviews make mistakes.** Some findings will be wrong, and some real bugs will be missed. Treat findings as leads to check, not verdicts. This action complements tests and static analysis; it doesn't replace them.
- **One review per repository.** Progress is shared across all runs in the repository, so run the action from one workflow only.

## License

[MIT](LICENSE)
