#!/usr/bin/env python3
import base64
import gzip
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SEVERITIES = ["high", "medium", "low"]
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LEVELS = {"high": "error", "medium": "warning", "low": "note"}
PROBLEM_SEVERITIES = {"high": "error", "medium": "warning", "low": "recommendation"}
CATEGORY = "ai-review"
TOOL_NAME = "AI Review"
TOOL_URL = "https://github.com/Mackery6969/AI-Review"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
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


class GeminiUnavailable(Exception):
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


def changed_paths(base, sha, include_deleted=False):
    filters = [] if include_deleted else ["--diff-filter=d"]
    out = git("diff", "--name-only", "--no-renames", *filters, "-z", base, sha)
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


def gemini_message(detail):
    try:
        message = json.loads(detail)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = detail
    return " ".join(str(message).split())[:300]


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
                reason = f"rate limited ({gemini_message(detail)})"
            elif e.code in (500, 502, 503, 504):
                transient += 1
                if transient > MAX_TRANSIENT_ATTEMPTS:
                    raise GeminiUnavailable(
                        f"Gemini returned {e.code} {MAX_TRANSIENT_ATTEMPTS} times in a row ({gemini_message(detail)})"
                    ) from None
                wait = min(30 * 2 ** (transient - 1), 600)
                reason = f"server error {e.code} ({gemini_message(detail)})"
            elif e.code in (401, 403, 404):
                sys.exit(f"::error::Gemini rejected the request ({e.code}); check the API key and model name. {detail[:500]}")
            else:
                raise ReviewFailed(f"Gemini returned {e.code}: {detail[:300]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            transient += 1
            if transient > MAX_TRANSIENT_ATTEMPTS:
                raise GeminiUnavailable(f"Gemini couldn't be reached {MAX_TRANSIENT_ATTEMPTS} times in a row") from None
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


def is_sha(value):
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def new_state():
    return {"version": 3, "baseline": None, "full_at": None, "findings": {}, "review": None, "upload_pending": False}


def valid_findings(findings):
    return isinstance(findings, dict) and all(
        isinstance(path, str) and isinstance(items, list) and all(isinstance(f, dict) for f in items)
        for path, items in findings.items()
    )


def valid_review(review):
    return (
        isinstance(review, dict)
        and is_sha(review.get("sha"))
        and (review.get("base") is None or is_sha(review["base"]))
        and all(isinstance(review.get(key), list) for key in ("done", "failed"))
        and valid_findings(review.get("findings"))
    )


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return new_state()
    except (OSError, ValueError):
        state = None
    # Commits from the state reach git, so everything is checked before use.
    if (
        isinstance(state, dict)
        and state.get("version") == 3
        and (state.get("baseline") is None or is_sha(state["baseline"]))
        and (state.get("full_at") is None or isinstance(state["full_at"], int))
        and valid_findings(state.get("findings"))
        and isinstance(state.get("upload_pending"), bool)
        and (state.get("review") is None or valid_review(state["review"]))
    ):
        return state
    print("::warning::The saved review state is unreadable, so this run starts over with a full review.", flush=True)
    return new_state()


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(temp, path)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write("state-changed=true\n")


def day(timestamp):
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def scope_text(review):
    if review["base"] is None:
        return f"the whole project at {review['sha'][:7]}"
    return f"changes from {review['base'][:7]} to {review['sha'][:7]}"


def attach_findings(findings, chunk, files, review):
    kept = []
    for f in findings:
        path = f["file"]
        if path not in chunk["files"]:
            print(f"  skipping a finding for {path!r}, which is not in this chunk", flush=True)
            continue
        lines = files[path].split("\n")
        line = min(max(f["line"], 1), len(lines))
        title = str(f.get("title", "")).strip()[:200] or "Possible defect"
        key = "\0".join([path, title.lower(), lines[line - 1].strip()])
        stored = {
            "severity": f["severity"],
            "line": line,
            "title": title,
            "explanation": str(f.get("explanation", "")).strip(),
            "fingerprint": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        }
        review["findings"].setdefault(path, []).append(stored)
        kept.append((path, stored))
    return kept


def build_sarif(findings, model):
    rules, results = {}, []
    for path in sorted(findings):
        for f in findings[path]:
            rule = "ai-review/" + hashlib.sha1(f["title"].lower().encode("utf-8")).hexdigest()[:12]
            rules.setdefault(rule, {
                "id": rule,
                "shortDescription": {"text": f["title"]},
                "fullDescription": {"text": f["title"]},
                "help": {"text": f"Reported by an AI code review using {model}. "
                                 "AI findings can be wrong, so check the code before acting on one."},
                "defaultConfiguration": {"level": LEVELS[f["severity"]]},
                "properties": {"problem.severity": PROBLEM_SEVERITIES[f["severity"]], "tags": ["ai-review"]},
            })
            results.append({
                "ruleId": rule,
                "level": LEVELS[f["severity"]],
                "message": {"text": f["explanation"] or f["title"]},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": urllib.parse.quote(path), "uriBaseId": "%SRCROOT%"},
                        "region": {"startLine": f["line"]},
                    }
                }],
                "partialFingerprints": {"aiReviewFinding/v1": f["fingerprint"]},
            })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": TOOL_NAME, "informationUri": TOOL_URL, "rules": list(rules.values())}},
            "automationDetails": {"id": f"{CATEGORY}/"},
            "results": results,
        }],
    }


def upload_sarif(gh, sarif, sha, ref):
    data = base64.b64encode(gzip.compress(json.dumps(sarif).encode("utf-8"))).decode("ascii")
    try:
        upload = gh.request("POST", "/code-scanning/sarifs", {
            "commit_sha": sha, "ref": ref, "sarif": data, "tool_name": TOOL_NAME,
        })
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        if e.code in (403, 404):
            sys.exit(f"::error::GitHub refused the code scanning upload ({e.code}). The workflow needs "
                     f"`security-events: write`, and code scanning must be available for this repository. {detail}")
        raise
    for _ in range(40):
        time.sleep(3)
        status = gh.request("GET", f"/code-scanning/sarifs/{upload['id']}")
        if status.get("processing_status") == "complete":
            return
        if status.get("processing_status") == "failed":
            sys.exit(f"::error::Code scanning could not process the results: {status.get('errors')}")
    print("::warning::Code scanning is still processing the results. They should appear shortly.", flush=True)


def publish(gh, state, state_path, model, ref):
    total = sum(len(items) for items in state["findings"].values())
    print(f"Uploading {plural(total, 'finding')} to code scanning", flush=True)
    upload_sarif(gh, build_sarif(state["findings"], model), state["baseline"], ref)
    state["upload_pending"] = False
    save_state(state_path, state)


def finish_review(state):
    review = state["review"]
    before = state["findings"]
    if review["base"] is None:
        state["findings"] = review["findings"]
        state["full_at"] = int(time.time())
    else:
        touched = changed_paths(review["base"], review["sha"], include_deleted=True)
        state["findings"] = {p: items for p, items in before.items() if p not in touched}
        state["findings"].update(review["findings"])
    state["baseline"] = review["sha"]
    state["review"] = None
    # A full review always uploads, so a reset replaces the old alerts even when nothing was found.
    if review["base"] is None or state["findings"] != before:
        state["upload_pending"] = True


def report(new_findings, gh, sha):
    if not new_findings:
        return
    order = {s: i for i, s in enumerate(SEVERITIES)}
    lines = ["", "| Severity | Location | Finding |", "| --- | --- | --- |"]
    for path, f in sorted(new_findings, key=lambda item: (order[item[1]["severity"]], item[0], item[1]["line"])):
        title = f["title"].replace("|", "\\|").replace("\n", " ")
        location = f"[`{path}:{f['line']}`]({gh.web}/blob/{sha}/{urllib.parse.quote(path)}#L{f['line']})"
        lines.append(f"| {f['severity'].capitalize()} | {location} | {title} |")
    summary("\n".join(lines) + "\n")


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
    model = setting("MODEL", "gemini-3.8-flash")
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
    ref = os.environ["GITHUB_REF"]
    state_path = os.path.join(setting("STATE_DIR", "."), "state.json")
    state = load_state(state_path)

    if setting("FULL_REVIEW", "false").lower() == "true":
        print("A full review was requested, so this run starts over.", flush=True)
        state = new_state()

    if state["upload_pending"]:
        print("The last finished review hasn't been uploaded yet; retrying.", flush=True)
        publish(gh, state, state_path, model, ref)

    if (
        state["review"] is None
        and reset_days > 0
        and state["full_at"]
        and time.time() - state["full_at"] >= reset_days * 86400
    ):
        print(f"The last full review was on {day(state['full_at'])}; starting a new one.", flush=True)
        state = new_state()

    if state["review"] is None:
        head = git("rev-parse", "HEAD").decode().strip()
        if head == state["baseline"]:
            summary(f"Nothing new to review: {head[:7]} was already reviewed.")
            return
        state["review"] = {"sha": head, "base": state["baseline"], "done": [], "failed": [], "findings": {}}
        save_state(state_path, state)

    review = state["review"]
    sha = review["sha"]
    ensure_commit(sha)
    only = None
    if review["base"]:
        try:
            ensure_commit(review["base"])
            only = changed_paths(review["base"], sha)
        except subprocess.CalledProcessError:
            print(f"::warning::The last reviewed commit {review['base'][:7]} no longer exists, probably because "
                  "history was rewritten. Reviewing the whole project instead.", flush=True)
            review["base"] = None
    files = load_files(sha, include, exclude, only)
    chunks = plan_chunks(files, limit)
    scope = scope_text(review)

    handled = set(review["done"]) | set(review["failed"])
    pending = [c for c in chunks if c["id"] not in handled]
    if chunks:
        print(f"Reviewing {scope}: {len(pending)} of {len(chunks)} chunks left", flush=True)

    allowed = SEVERITIES[: SEVERITIES.index(min_severity) + 1]
    new_findings, reviewed, stop_reason, last_request = [], 0, None, 0.0
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
        except GeminiUnavailable as e:
            # An outage says nothing about the code, so the chunk is retried on the next run rather than failed.
            stop_reason = str(e)
            break
        except ReviewFailed as e:
            print(f"::warning::Review of {chunk['label']} failed: {' '.join(str(e).split())}", flush=True)
            review["failed"].append(chunk["id"])
        else:
            kept = attach_findings([f for f in findings if f["severity"] in allowed], chunk, files, review)
            print(f"  {plural(len(kept), 'finding')}", flush=True)
            new_findings += kept
            review["done"].append(chunk["id"])

        reviewed += 1
        save_state(state_path, state)

    remaining = len(pending) - reviewed
    if remaining:
        summary(f"Reviewed {plural(reviewed, 'chunk')} of {scope} this run, then stopped because {stop_reason}. "
                f"The remaining {plural(remaining, 'chunk')} will be reviewed on the next run.")
        report(new_findings, gh, sha)
        return

    finish_review(state)
    save_state(state_path, state)
    if not chunks:
        summary(f"Nothing to send to Gemini: no files matching include/exclude in {scope}.")
    else:
        summary(f"Finished reviewing {scope}: {plural(len(new_findings), 'new finding')} this run.")
        report(new_findings, gh, sha)
    if state["upload_pending"]:
        publish(gh, state, state_path, model, ref)
    else:
        print("Findings are unchanged, so nothing was uploaded.", flush=True)


if __name__ == "__main__":
    main()
