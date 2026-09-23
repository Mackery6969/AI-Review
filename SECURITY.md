# Security Policy

## Supported versions

Only the latest release of the current major version receives security fixes. If you use `@v2`, you get fixes automatically. If you pin a commit SHA or an exact tag, update to the latest release.

| Version | Supported |
| --- | --- |
| Latest `v2.x` | Yes |
| Older releases | No |

## Reporting a vulnerability

**Please don't report security problems in public issues, discussions, or pull requests.**

Report them privately through GitHub instead: open the repository's **Security** tab and choose **Report a vulnerability**. Only you and the maintainer can see the report.

Please include:

- what the problem is and what an attacker could do with it,
- steps to reproduce it, or a minimal workflow that shows it,
- the version or commit you tested,
- a suggested fix, if you have one.

This project is maintained by one person on a best-effort basis. You'll get a reply once the report has been looked at. Valid reports get fixed in a new release, and you'll be credited in the advisory unless you'd rather not be.

## What counts as a vulnerability

The action runs inside other people's workflows with their API key and a GitHub token, so the most important issues are ones that:

- leak the Gemini API key or the GitHub token, for example into logs, uploaded results, or requests to the wrong host,
- let repository content, model output, or tampered cached progress run commands, inject workflow commands, or reach `git` as options,
- let model output do more on GitHub than create inert code scanning alerts,
- use the token for anything beyond uploading code scanning results.

These are **not** vulnerabilities in this action:

- inaccurate, missing, or misleading review findings, which are expected from an AI reviewer,
- your code being sent to Google, which is how the action works and is explained in the README,
- problems in Gemini, GitHub Actions, or GitHub itself. Report those to Google or GitHub.

## How the action limits its own risk

- It needs only `contents: read` and `security-events: write`, and the only thing it writes to GitHub is code scanning results. Progress is kept in the Actions cache, which needs no extra permission.
- It uses only the Python standard library. Its only other dependencies are GitHub's own `actions/cache` actions, which Dependabot keeps up to date along with this repository's workflows.
- It treats everything the model returns as untrusted. Findings are uploaded as plain-text results that can't run anything or notify anyone, and they're never printed as workflow commands.
- It validates the progress restored from the cache before using any of it, so tampered or corrupted state can't pass arbitrary values to `git`. Anything unexpected is discarded and a fresh full review starts.
- It never prints the API key or the token.
