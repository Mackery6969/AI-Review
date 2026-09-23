#!/usr/bin/env python3
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

LABEL = "ai-review"
SEVERITIES = ["high", "medium", "low"]
STATE_RE = re.compile(r"<!-- ai-review-state (\{.*?\}) -->", re.S)
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_BODY = 65000
MAX_TRANSIENT_ATTEMPTS = 5

SYSTEM_PROMPT = """You are a senior engineer reviewing part of a codebase.
Report real defects only: bugs, logic errors, crashes, null or bounds problems, resource leaks,
concurrency problems, security issues, and code that clearly will not do what its author intended.
Do not report style, formatting, naming, missing comments, or speculative refactors.
Only report an issue when you are confident it is real; an empty list is a good answer.
Every file is shown with line numbers. Cite the exact file path and line the problem is on.
Keep each explanation short and concrete: what goes wrong, and when."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "findings": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "file": {"type": "STRING"},
                    "line": {"type": "INTEGER"},
                    "severity": {"type": "STRING", "enum": SEVERITIES},
                    "title": {"type": "STRING"},
                    "explanation": {"type": "STRING"},
                },
                "required": ["file", "line", "severity", "title", "explanation"],
            },
        }
    },
    "required": ["findings"],
}


class DailyQuotaExhausted(Exception):
    pass


class OutOfTime(Exception):
    pass


class ReviewFailed(Exception):
    pass


def setting(name, default=""):
    return os.environ.get("AIR_" + name, "").strip() or default


def split_list(value):
    return [p.strip() for p in re.split(r"[,\n]", value) if p.strip()]


def glob_to_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def ensure_commit(sha):
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode != 0:
        git("fetch", "--depth=1", "origin", sha)


def changed_paths(base, sha):
    out = git("diff", "--name-only", "--no-renames", "--diff-filter=d", "-z", base, sha)
    return {p for p in out.decode("utf-8").split("\0") if p}


def load_files(sha, include, exclude, only=None):
    inc = [glob_to_regex(p) for p in include]
    exc = [glob_to_regex(p) for p in exclude]
    files = {}
    for entry in git("ls-tree", "-r", "-z", sha).decode("utf-8").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, kind, _ = meta.split(" ")
        if kind != "blob" or mode == "120000":
            continue
        if only is not None and path not in only:
            continue
        if not any(r.match(path) for r in inc) or any(r.match(path) for r in exc):
            continue
        data = git("show", f"{sha}:{path}")
        if b"\0" in data:
            continue
        files[path] = data.decode("utf-8", errors="replace")
    return files


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def line_count(text):
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def plan_chunks(files, limit):
    groups, current, size = [], [], 0
    for path in sorted(files):
        n = line_count(files[path])
        if current and size + n > limit:
            groups.append(current)
            current, size = [], 0
        current.append(path)
        size += n
    if current:
        groups.append(current)

    chunks = []
    for group in groups:
        if len(group) == 1:
            label = group[0]
        else:
            base = posixpath.commonpath([posixpath.dirname(p) for p in group])
            first, last = (posixpath.relpath(p, base or ".") for p in (group[0], group[-1]))
            label = f"{base or '.'}/ {first} … {last}"
        chunks.append({
            "id": hashlib.sha1(group[0].encode("utf-8")).hexdigest()[:10],
            "files": group,
            "label": label,
            "lines": sum(line_count(files[p]) for p in group),
        })
    return chunks


def build_prompt(chunk, files, instructions):
    parts = []
    if instructions:
        parts.append(f"Project context: {instructions}\n")
    parts.append(f"Review these {len(chunk['files'])} files.\n")
    for path in chunk["files"]:
        lines = files[path].split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        width = len(str(len(lines)))
        numbered = "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, 1))
        parts.append(f"=== {path} ===\n{numbered}\n")
    return "\n".join(parts)


def retry_delay(detail, headers):
    match = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', detail)
    if match:
        return float(match.group(1)) + 1
    header = headers.get("Retry-After") if headers else None
    if header and header.isdigit():
        return float(header)
    return None


def call_gemini(model, api_key, prompt, deadline):
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }).encode("utf-8")
    url = GEMINI_URL.format(model=model)
    rate_limited, transient = 0, 0

    while True:
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        })
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                if "PerDay" in detail:
                    raise DailyQuotaExhausted() from None
                rate_limited += 1
                wait = retry_delay(detail, e.headers) or min(60 * 2 ** (rate_limited - 1), 900)
                reason = "rate limited"
            elif e.code in (500, 502, 503, 504):
                transient += 1
                if transient > MAX_TRANSIENT_ATTEMPTS:
                    raise ReviewFailed(f"Gemini returned {e.code} {MAX_TRANSIENT_ATTEMPTS} times: {detail[:300]}") from None
                wait = min(30 * 2 ** (transient - 1), 600)
                reason = f"server error {e.code}"
            elif e.code in (401, 403, 404):
                sys.exit(f"::error::Gemini rejected the request ({e.code}); check the API key and model name. {detail[:500]}")
            else:
                raise ReviewFailed(f"Gemini returned {e.code}: {detail[:300]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            transient += 1
            if transient > MAX_TRANSIENT_ATTEMPTS:
                raise ReviewFailed(f"network error {MAX_TRANSIENT_ATTEMPTS} times: {e}") from None
            wait = min(30 * 2 ** (transient - 1), 600)
            reason = f"network error ({e})"

        if time.time() + wait > deadline:
            raise OutOfTime()
        print(f"  {reason}; waiting {wait:.0f}s", flush=True)
        time.sleep(wait)


def parse_findings(response):
    candidate = (response.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    try:
        findings = json.loads(text)["findings"]
    except (ValueError, KeyError, TypeError):
        feedback = response.get("promptFeedback")
        raise ReviewFailed(
            f"unreadable response (finish reason {candidate.get('finishReason')}"
            + (f", prompt feedback {feedback}" if feedback else "") + ")"
        ) from None
    if not isinstance(findings, list):
        raise ReviewFailed("response did not contain a list of findings")
    findings = [f for f in findings if isinstance(f, dict)]
    for f in findings:
        f["severity"] = str(f.get("severity", "low")).lower()
        if f["severity"] not in SEVERITIES:
            f["severity"] = "low"
        f["file"] = str(f.get("file", ""))
        try:
            f["line"] = max(int(f.get("line", 0)), 0)
        except (TypeError, ValueError):
            f["line"] = 0
    return findings


class GitHub:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.web = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"

    def request(self, method, path, data=None):
        request = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}",
            data=None if data is None else json.dumps(data).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "ai-review-action",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def ensure_label(self):
        try:
            self.request("POST", "/labels", {
                "name": LABEL,
                "color": "8250df",
                "description": "Tracking issue for a paced AI code review",
            })
        except urllib.error.HTTPError as e:
            if e.code != 422:
                raise

    def find_tracking_issue(self):
        for issue in self.request("GET", f"/issues?labels={LABEL}&state=open&per_page=100") or []:
            if "pull_request" not in issue and STATE_RE.search(issue.get("body") or ""):
                return issue
        return None


def is_sha(value):
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def valid_review(review):
    return (
        isinstance(review, dict)
        and is_sha(review.get("sha"))
        and (review.get("base") is None or is_sha(review["base"]))
        and isinstance(review.get("findings"), int)
        and all(
            isinstance(review.get(key), list) and all(isinstance(i, str) for i in review[key])
            for key in ("done", "failed")
        )
    )


def load_state(body):
    # The issue body is editable by anyone with write access, so check every field before it reaches git.
    try:
        state = json.loads(STATE_RE.search(body).group(1))
    except (AttributeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if state.get("version") == 1:
        review = {k: state.get(k) for k in ("sha", "done", "failed", "findings")}
        review["base"] = None
        return new_state(review) if valid_review(review) else None
    baseline, review, full_at = state.get("baseline"), state.get("review"), state.get("full_at")
    if (
        (baseline is None or is_sha(baseline))
        and (review is None or valid_review(review))
        and (full_at is None or (isinstance(full_at, int) and not isinstance(full_at, bool)))
    ):
        return {"version": 2, "baseline": baseline, "full_at": full_at, "review": review}
    return None


def new_state(review=None):
    return {"version": 2, "baseline": None, "full_at": None, "review": review}


def finish_review(state):
    review = state["review"]
    state["baseline"] = review["sha"]
    if review["base"] is None:
        state["full_at"] = int(time.time())
    state["review"] = None


def day(timestamp):
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def commit_link(sha, web):
    return f"[`{sha[:7]}`]({web}/tree/{sha})" if web else sha[:7]


def scope_text(review, web=None):
    if review["base"] is None:
        return f"the whole project at {commit_link(review['sha'], web)}"
    return f"changes from {commit_link(review['base'], web)} to {commit_link(review['sha'], web)}"


def render_body(state, chunks, model, web, reset_days):
    review = state["review"]
    baseline = state["baseline"]
    status = [f"**Last completed review:** {commit_link(baseline, web) if baseline else 'none yet'}"]
    if state["full_at"]:
        full = f"**Last full review:** {day(state['full_at'])}"
        if reset_days > 0:
            full += f". The whole project is reviewed again after {day(state['full_at'] + reset_days * 86400)}."
        status.append(full)
    if review:
        done, failed = set(review["done"]), set(review["failed"])
        processed = sum(1 for c in chunks if c["id"] in done or c["id"] in failed)
        status.append(
            f"**Current review:** {scope_text(review, web)}. {processed} of {len(chunks)} chunks reviewed, "
            f"{plural(review['findings'], 'finding')} so far."
        )
    else:
        status.append("**Current review:** none. The next run reviews anything changed since the last completed review.")

    lines = [
        f"<!-- ai-review-state {json.dumps(state, separators=(',', ':'))} -->",
        f"Ongoing AI code review using `{model}`. The first review covers the whole project. "
        "After that, each review covers only the files changed since the last one finished.",
        "",
        "\n\n".join(status),
        "",
        "Findings are posted as comments below. If a run stops on quota or time, the next run picks up where it left off. "
        "Close this issue to make the next run review the whole project again, in a new issue.",
    ]
    header = "\n".join(lines)
    if not review or not chunks:
        return header

    checklist = ["", "<details><summary>Chunks in the current review</summary>", ""]
    for c in chunks:
        if c["id"] in failed:
            checklist.append(f"- [ ] `{c['label']}` ({plural(len(c['files']), 'file')}) — review failed, see comments")
        else:
            mark = "x" if c["id"] in done else " "
            checklist.append(f"- [{mark}] `{c['label']}` ({plural(len(c['files']), 'file')})")
    checklist += ["", "</details>"]
    body = header + "\n" + "\n".join(checklist)
    return body if len(body) <= MAX_BODY else header


def neutralize(text):
    # Model output is untrusted; stop it from @-mentioning users or cross-linking issues outside code spans.
    parts = re.split(r"(`[^`\n]*`)", str(text))
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"@(?=\w)", "@​", parts[i])
        parts[i] = re.sub(r"#(?=\d)", "#​", parts[i])
    return "".join(parts)


def render_findings(chunk, findings, gh, sha):
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings = sorted(findings, key=lambda f: (order[f["severity"]], f["file"], f["line"]))
    lines = [f"### `{chunk['label']}` at `{sha[:7]}` — {plural(len(findings), 'finding')}", ""]
    for f in findings:
        path, line = f["file"], f["line"]
        where = f"`{path.replace('`', '')}:{line}`"
        if path in chunk["files"]:
            where = f"[{where}]({gh.web}/blob/{sha}/{path}#L{line})"
        lines.append(f"**{f['severity'].capitalize()}** · {where} — {neutralize(f.get('title', '')).strip()}")
        lines.append("")
        lines.append(neutralize(f.get("explanation", "")).strip())
        lines.append("")
    body = "\n".join(lines)
    return body if len(body) <= MAX_BODY else body[:MAX_BODY] + "\n\n_(truncated)_"


def summary(text):
    print(text, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def dry_run(chunks):
    total_lines = sum(c["lines"] for c in chunks)
    for i, c in enumerate(chunks, 1):
        print(f"{i:>4}. {c['label']}  ({len(c['files'])} files, {c['lines']} lines)")
    summary(f"Dry run: {sum(len(c['files']) for c in chunks)} files, {total_lines} lines, "
            f"{len(chunks)} chunks (one Gemini request each).")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    include = split_list(setting("INCLUDE", "**/*.java"))
    exclude = split_list(setting("EXCLUDE"))
    limit = int(setting("CHUNK_LINES", "3000"))
    model = setting("MODEL", "gemini-2.5-flash")
    min_severity = setting("MIN_SEVERITY", "medium").lower()
    if min_severity not in SEVERITIES:
        sys.exit(f"::error::min-severity must be one of {', '.join(SEVERITIES)}")
    interval = 60 / float(setting("REQUESTS_PER_MINUTE", "5"))
    deadline = time.time() + float(setting("TIME_BUDGET_MINUTES", "330")) * 60
    instructions = setting("INSTRUCTIONS")
    reset_days = float(setting("RESET_AFTER_DAYS", "30"))

    if setting("DRY_RUN", "false").lower() == "true":
        sha = git("rev-parse", "HEAD").decode().strip()
        dry_run(plan_chunks(load_files(sha, include, exclude), limit))
        return

    api_key = setting("GEMINI_API_KEY")
    if not api_key:
        sys.exit("::error::gemini-api-key is empty. Fork pull requests and Dependabot runs do not receive secrets.")
    gh = GitHub(setting("GITHUB_TOKEN"), os.environ["GITHUB_REPOSITORY"])

    issue = gh.find_tracking_issue()
    state = new_state()
    previous_issue = None
    if issue:
        state = load_state(issue["body"])
        if state is None:
            sys.exit(f"::error::The progress saved in issue #{issue['number']} is damaged. "
                     "Close the issue to start a new review.")
        print(f"Using tracking issue #{issue['number']}", flush=True)

    if (
        state["review"] is None
        and reset_days > 0
        and state["full_at"]
        and time.time() - state["full_at"] >= reset_days * 86400
    ):
        print(f"Last full review was on {day(state['full_at'])}; starting a new full review.", flush=True)
        previous_issue, issue, state = issue, None, new_state()

    if state["review"] is None:
        head = git("rev-parse", "HEAD").decode().strip()
        if head == state["baseline"]:
            summary(f"Nothing new to review: {head[:7]} was already reviewed.")
            return
        state["review"] = {"sha": head, "base": state["baseline"], "done": [], "failed": [], "findings": 0}

    review = state["review"]
    sha = review["sha"]
    ensure_commit(sha)
    only = None
    if review["base"]:
        try:
            ensure_commit(review["base"])
            only = changed_paths(review["base"], sha)
        except subprocess.CalledProcessError:
            print(f"::warning::The last reviewed commit {review['base'][:7]} no longer exists, "
                  "probably because history was rewritten. Reviewing the whole project instead.", flush=True)
            review["base"] = None
    files = load_files(sha, include, exclude, only)
    chunks = plan_chunks(files, limit)
    scope = scope_text(review)

    if not chunks:
        if issue:
            finish_review(state)
            gh.request("PATCH", f"/issues/{issue['number']}", {"body": render_body(state, [], model, gh.web, reset_days)})
        summary(f"No files matching the include/exclude patterns in {scope}; nothing to review.")
        return

    if not issue:
        gh.ensure_label()
        issue = gh.request("POST", "/issues", {
            "title": "AI code review",
            "body": render_body(state, chunks, model, gh.web, reset_days),
            "labels": [LABEL],
        })
        print(f"Started tracking issue #{issue['number']}", flush=True)
        if previous_issue:
            old = previous_issue["number"]
            gh.request("POST", f"/issues/{old}/comments", {
                "body": f"Starting a new full review in #{issue['number']}, since the last one is over "
                        f"{reset_days:g} days old.",
            })
            gh.request("PATCH", f"/issues/{old}", {"state": "closed", "state_reason": "completed"})

    number = issue["number"]
    handled = set(review["done"]) | set(review["failed"])
    pending = [c for c in chunks if c["id"] not in handled]
    print(f"Reviewing {scope}: {len(pending)} of {len(chunks)} chunks left", flush=True)

    allowed = SEVERITIES[: SEVERITIES.index(min_severity) + 1]
    reviewed, stop_reason, last_request = 0, None, 0.0
    for chunk in pending:
        wait = last_request + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        if time.time() >= deadline:
            stop_reason = "the time budget ran out"
            break
        last_request = time.time()
        print(f"Reviewing {chunk['label']} ({len(chunk['files'])} files, {chunk['lines']} lines)", flush=True)

        try:
            findings = parse_findings(call_gemini(model, api_key, build_prompt(chunk, files, instructions), deadline))
        except DailyQuotaExhausted:
            stop_reason = "Gemini's daily quota ran out"
            break
        except OutOfTime:
            stop_reason = "the time budget ran out"
            break
        except ReviewFailed as e:
            print(f"  failed: {e}", flush=True)
            gh.request("POST", f"/issues/{number}/comments", {
                "body": f"### `{chunk['label']}` — review failed\n\n{neutralize(e)}",
            })
            review["failed"].append(chunk["id"])
        else:
            findings = [f for f in findings if f["severity"] in allowed]
            print(f"  {len(findings)} findings", flush=True)
            if findings:
                gh.request("POST", f"/issues/{number}/comments", {"body": render_findings(chunk, findings, gh, sha)})
            review["done"].append(chunk["id"])
            review["findings"] += len(findings)

        reviewed += 1
        gh.request("PATCH", f"/issues/{number}", {"body": render_body(state, chunks, model, gh.web, reset_days)})

    remaining = len(pending) - reviewed
    if remaining:
        summary(f"Reviewed {reviewed} chunks this run; stopped because {stop_reason}. "
                f"{remaining} chunks remain in #{number} and will resume on the next run.")
        return

    total_findings = review["findings"]
    finish_review(state)
    gh.request("PATCH", f"/issues/{number}", {"body": render_body(state, [], model, gh.web, reset_days)})
    summary(f"Finished reviewing {scope} in #{number} ({plural(total_findings, 'finding')}).")


if __name__ == "__main__":
    main()
