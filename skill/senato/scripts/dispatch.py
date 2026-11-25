#!/usr/bin/env python3
"""Senato read-only multi-CLI seat dispatcher (Python 3.9+ standard library only).

Config discovery: --config > $SENATO_CONFIG > ~/.config/senato/config.json
(legacy fallback: ~/.config/council/config.json when only that file exists).

Usage:
  python dispatch.py --doctor [--config PATH]
  python dispatch.py --run JOBS.json [--config PATH] [--workspace REPO]
                     [--out RESULT.json] [--keep-temp]

Exit codes: 0 = every seat succeeded and the workspace is clean;
1 = partial or total seat failure; 2 = config, security or workspace violation.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ARG_PAYLOAD_LIMIT = 24576
MAX_RETRIES = 1  # retry_on_stderr_regex never retries a seat more than once
INPUT_MODES = {"stdin", "arg", "file"}
OUTPUT_MODES = {"stdout", "file"}
PLACEHOLDER_RE = re.compile(r"\{(prompt_file|prompt_text|output_file|temp_dir)\}")
IS_WINDOWS = os.name == "nt"

# Flags that are forbidden regardless of value.
FORBIDDEN_BARE = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--full-auto",
    "--approve-for-me",
    "--dangerously-bypass-hook-trust",
}
WRITE_SANDBOXES = {"workspace-write", "danger-full-access"}
# Flags that are forbidden only with one of these (case-insensitive) values.
FORBIDDEN_VALUED = {
    "--permission-mode": {"acceptedits", "bypasspermissions", "auto"},
    "-s": WRITE_SANDBOXES,
    "--sandbox": WRITE_SANDBOXES,
    "--mode": {"accept-edits", "auto"},
}
# Config-override flags whose value is a key=value pair (Codex style).
CONFIG_FLAGS = {"-c", "--config"}
# Tool-allowlist flags; any listed write/shell tool is forbidden.
ALLOWED_TOOLS_FLAGS = {"--allowedtools", "--allowed-tools"}
WRITE_TOOLS = {"edit", "write", "bash", "powershell", "notebookedit", "multiedit"}
TOOL_SPLIT_RE = re.compile(r"[,\s]+")
# OS variables kept when an adapter declares an env_allowlist.
BASE_ENV_KEYS = (
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMROOT",
    "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "USER", "USERNAME",
    "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TERM",
)


class DispatchError(Exception):
    """Structured error carrying a machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self):
        return {"code": self.code, "message": self.message}


def resolve_config_path(cli_path=None, home=None):
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("SENATO_CONFIG")
    if env_path:
        return Path(env_path)
    base = Path(home) if home is not None else Path.home()
    primary = base / ".config" / "senato" / "config.json"
    legacy = base / ".config" / "council" / "config.json"
    if not primary.exists() and legacy.is_file():
        return legacy
    return primary


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _config_sets_write_sandbox(value):
    """True if a `key=value` config override sets sandbox_mode to a write mode."""
    key, eq, val = value.partition("=")
    return bool(eq) and _unquote(key) == "sandbox_mode" and _unquote(val) in WRITE_SANDBOXES


def _write_tools(values):
    """Return the write/shell tool names granted by allowlist values."""
    found = []
    for value in values:
        for item in TOOL_SPLIT_RE.split(value):
            name = _unquote(item.split("(", 1)[0])
            if name in WRITE_TOOLS:
                found.append(name)
    return found


def find_forbidden_flags(args):
    """Return a list of forbidden flag descriptions found in an argv template."""
    hits = []
    raw = [str(a) for a in args]
    lowered = [a.strip().lower() for a in raw]
    nxt = lambda i: lowered[i + 1] if i + 1 < len(lowered) else None
    for i, tok in enumerate(lowered):
        flag, eq, value = tok.partition("=")
        short = tok[:2] if tok.startswith("-") and not tok.startswith("--") and len(tok) > 2 else None
        if tok in FORBIDDEN_BARE or (eq and flag in FORBIDDEN_BARE):
            hits.append(raw[i])
        elif flag in FORBIDDEN_VALUED:
            if eq:
                if value in FORBIDDEN_VALUED[flag]:
                    hits.append(raw[i])
            elif nxt(i) in FORBIDDEN_VALUED[flag]:
                hits.append("%s %s" % (raw[i], raw[i + 1]))
        elif short == "-s" and tok[2:].lstrip("=") in WRITE_SANDBOXES:
            hits.append(raw[i])  # attached short form, e.g. -sworkspace-write
        elif flag in CONFIG_FLAGS or short == "-c":
            cfg, shown = value, raw[i]  # --config=k=v
            if flag not in CONFIG_FLAGS:
                cfg = tok[2:].lstrip("=")  # attached short form, e.g. -csandbox_mode=...
            elif not eq and nxt(i) is not None:
                cfg, shown = nxt(i), "%s %s" % (raw[i], raw[i + 1])
            if _config_sets_write_sandbox(cfg):
                hits.append(shown)
        elif flag in ALLOWED_TOOLS_FLAGS:
            if eq:
                values, shown = [value], [raw[i]]
            else:  # variadic: every following token up to the next flag
                end = i + 1
                while end < len(lowered) and not lowered[end].startswith("-"):
                    end += 1
                values, shown = lowered[i + 1:end], raw[i:end]
            if _write_tools(values):
                hits.append(" ".join(shown))
    return hits


def validate_adapter(name, spec, default_timeout):
    if not isinstance(spec, dict):
        raise DispatchError("INVALID_CONFIG", "adapter %r must be an object" % name)
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        raise DispatchError("INVALID_CONFIG", "adapter %r needs a non-empty 'command'" % name)
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise DispatchError("INVALID_CONFIG", "adapter %r 'args' must be a list of strings" % name)
    in_mode, out_mode = spec.get("input_mode"), spec.get("output_mode")
    if in_mode not in INPUT_MODES:
        raise DispatchError("INVALID_CONFIG", "adapter %r has invalid input_mode %r" % (name, in_mode))
    if out_mode not in OUTPUT_MODES:
        raise DispatchError("INVALID_CONFIG", "adapter %r has invalid output_mode %r" % (name, out_mode))
    timeout = spec.get("timeout_sec", default_timeout)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise DispatchError("INVALID_CONFIG", "adapter %r needs a positive timeout_sec" % name)
    joined = "\n".join(args)
    required = {"file": "{prompt_file}", "arg": "{prompt_text}"}.get(in_mode)
    for ph in (required, "{output_file}" if out_mode == "file" else None):
        if ph and ph not in joined:
            raise DispatchError("PLACEHOLDER_MISSING", "adapter %r requires %s in args" % (name, ph))
    allow = spec.get("env_allowlist")
    if allow is not None and not (isinstance(allow, list) and all(isinstance(v, str) for v in allow)):
        raise DispatchError("INVALID_CONFIG", "adapter %r 'env_allowlist' must be a list of strings" % name)
    extra = spec.get("extra_env")
    if extra is not None and not (isinstance(extra, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in extra.items())):
        raise DispatchError("INVALID_CONFIG", "adapter %r 'extra_env' must map strings to strings" % name)
    retry_re = spec.get("retry_on_stderr_regex")
    if retry_re is not None:
        if not isinstance(retry_re, str) or not retry_re:
            raise DispatchError("INVALID_CONFIG",
                                "adapter %r 'retry_on_stderr_regex' must be a non-empty string" % name)
        try:
            re.compile(retry_re)
        except re.error as exc:
            raise DispatchError("INVALID_CONFIG",
                                "adapter %r 'retry_on_stderr_regex' is not a valid regex: %s" % (name, exc))
    hits = find_forbidden_flags(args)
    if hits:
        raise DispatchError("FORBIDDEN_WRITE_FLAG", "adapter %r uses forbidden flag(s): %s" % (name, ", ".join(hits)))
    out = dict(spec)
    out["args"], out["timeout_sec"] = list(args), float(timeout)
    return out


def validate_config(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise DispatchError("INVALID_CONFIG", "config must be an object with version == 1")
    adapters = data.get("adapters")
    if not isinstance(adapters, dict) or not adapters:
        raise DispatchError("INVALID_CONFIG", "config 'adapters' must be a non-empty object")
    default_timeout = data.get("default_timeout_sec", 180)
    return {name: validate_adapter(name, spec, default_timeout) for name, spec in adapters.items()}


def read_json(path, label):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise DispatchError("%s_NOT_FOUND" % label, "%s file not found: %s" % (label.lower(), path))
    except (OSError, ValueError) as exc:
        raise DispatchError("INVALID_%s" % label, "cannot read %s %s: %s" % (label.lower(), path, exc))


def load_config(cli_path=None):
    return validate_config(read_json(resolve_config_path(cli_path), "CONFIG"))


def resolve_command(command):
    """Resolve to a full path; wrap PowerShell shims. Returns argv prefix or None."""
    path = shutil.which(command)
    if path is None:
        return None
    if IS_WINDOWS and path.lower().endswith(".ps1"):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            return None
        return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path]
    return [path]


def is_batch_shim(argv0):
    return IS_WINDOWS and argv0.lower().endswith((".cmd", ".bat"))


def build_env(spec):
    allow = spec.get("env_allowlist")
    if allow is None:
        env = dict(os.environ)
    else:
        upper = {k.upper(): k for k in os.environ}
        env = {}
        for key in list(BASE_ENV_KEYS) + list(allow):
            real = upper.get(key.upper())
            if real is not None:
                env[real] = os.environ[real]
    env.update(spec.get("extra_env") or {})
    return env


def kill_tree(proc):
    """Terminate a process and all of its descendants."""
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def substitute(args, values):
    """Single-pass placeholder substitution (prompt text is never re-scanned)."""
    return [PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], a) for a in args]


def tail(data, limit=2000):
    text = data.decode("utf-8", errors="replace").strip() if data else ""
    return text[-limit:]


def run_seat(job, adapters, keep_temp=False):
    seat_id = job.get("seat_id") or "seat"
    name = job.get("adapter")
    result = {"seat_id": seat_id, "adapter": name, "status": "error", "exit_code": None,
              "duration_sec": 0.0, "attempts": 1, "output": None, "error": None}
    start = time.monotonic()

    def fail(status, code, message):
        result.update(status=status, error={"code": code, "message": message},
                      duration_sec=round(time.monotonic() - start, 3))
        return result

    spec = adapters.get(name)
    if spec is None:
        return fail("rejected", "UNKNOWN_ADAPTER", "adapter %r is not configured" % name)
    try:
        prompt = load_prompt(job)
    except DispatchError as exc:
        return fail("rejected", exc.code, exc.message)
    if spec["input_mode"] == "arg" and len(prompt.encode("utf-8")) > ARG_PAYLOAD_LIMIT:
        return fail("rejected", "ARG_PAYLOAD_TOO_LARGE",
                    "arg-mode prompt exceeds %d UTF-8 bytes; use stdin or file mode" % ARG_PAYLOAD_LIMIT)
    prefix = resolve_command(spec["command"])
    if prefix is None:
        return fail("error", "COMMAND_NOT_FOUND", "executable %r not found on PATH" % spec["command"])
    if spec["input_mode"] == "arg" and is_batch_shim(prefix[0]):
        return fail("rejected", "BATCH_SHIM_ARG_UNSAFE",
                    "arg mode through a .cmd/.bat shim is unsafe; use stdin or file mode")

    retry_re = spec.get("retry_on_stderr_regex")
    for attempt in (1, 2):  # at most MAX_RETRIES (1) retry after the first attempt
        result["attempts"] = attempt
        outcome = run_attempt(spec, prefix, prompt, seat_id, keep_temp)
        result["exit_code"] = outcome["exit_code"]
        if outcome["temp_dir"] is not None:
            result["temp_dir"] = outcome["temp_dir"]
        if outcome["status"] == "ok":
            result.update(status="ok", output=outcome["output"], error=None,
                          duration_sec=round(time.monotonic() - start, 3))
            return result
        retryable = (outcome["code"] == "NONZERO_EXIT" and retry_re is not None
                     and attempt <= MAX_RETRIES
                     and re.search(retry_re, outcome["stderr"]) is not None)
        if not retryable:
            return fail(outcome["status"], outcome["code"], outcome["message"])
    return result  # unreachable: the second attempt is never retryable


def run_attempt(spec, prefix, prompt, seat_id, keep_temp=False):
    """Run one isolated attempt of a seat. Returns a plain outcome dict."""
    outcome = {"status": "error", "code": None, "message": None, "exit_code": None,
               "output": None, "stderr": "", "temp_dir": None}

    def fail(status, code, message):
        outcome.update(status=status, code=code, message=message)
        return outcome

    temp_dir = tempfile.mkdtemp(prefix="senato_%s_" % re.sub(r"[^A-Za-z0-9_-]", "_", seat_id))
    outcome["temp_dir"] = temp_dir
    try:
        values = {"temp_dir": temp_dir, "prompt_text": prompt,
                  "prompt_file": os.path.join(temp_dir, "prompt.txt"),
                  "output_file": os.path.join(temp_dir, "output.txt")}
        if spec["input_mode"] == "file":
            with open(values["prompt_file"], "w", encoding="utf-8", newline="") as fh:
                fh.write(prompt)
        argv = prefix + substitute(spec["args"], values)
        stdin_bytes = prompt.encode("utf-8") if spec["input_mode"] == "stdin" else b""
        popen_kw = {"cwd": temp_dir, "env": build_env(spec), "shell": False,
                    "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if IS_WINDOWS:
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, **popen_kw)
        except OSError as exc:
            return fail("error", "SPAWN_FAILED", str(exc))
        try:
            out, err = proc.communicate(stdin_bytes, timeout=spec["timeout_sec"])
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            return fail("timeout", "TIMEOUT", "seat exceeded %gs; process tree killed" % spec["timeout_sec"])
        outcome["exit_code"] = proc.returncode
        outcome["stderr"] = err.decode("utf-8", errors="replace") if err else ""
        if proc.returncode != 0:
            return fail("error", "NONZERO_EXIT", "exit %d: %s" % (proc.returncode, tail(err)))
        if spec["output_mode"] == "file":
            try:
                with open(values["output_file"], "r", encoding="utf-8-sig", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                return fail("error", "OUTPUT_MISSING", "adapter did not write its output file")
        else:
            text = out.decode("utf-8", errors="replace")
        if not text.strip():
            return fail("error", "EMPTY_OUTPUT", "adapter returned empty output: %s" % tail(err))
        outcome.update(status="ok", output=text)
        return outcome
    finally:
        if not keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _git(workspace, *args):
    proc = subprocess.run(["git", "-C", str(workspace)] + list(args), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    return proc.returncode, proc.stdout


def workspace_snapshot(workspace):
    """Return a fingerprint of the repo state, or None if not inside a git work tree."""
    if workspace is None or shutil.which("git") is None:
        return None
    try:
        rc, out = _git(workspace, "rev-parse", "--is-inside-work-tree")
        if rc != 0 or out.strip() != b"true":
            return None
        rc, status = _git(workspace, "status", "--porcelain", "--untracked-files=all")
        if rc != 0:
            return None
        _, diff = _git(workspace, "diff", "--no-ext-diff", "--binary", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    return status + b"\0" + hashlib.sha256(diff).hexdigest().encode("ascii")


def write_report(report, out_path=None):
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.flush()


def cmd_doctor(config_path):
    path = resolve_config_path(config_path)
    report = {"config_path": str(path), "valid": False, "adapters": {}, "usable": 0, "error": None}
    try:
        raw = read_json(path, "CONFIG")
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("adapters"), dict):
            raise DispatchError("INVALID_CONFIG", "config must be an object with version == 1 and 'adapters'")
    except DispatchError as exc:
        report["error"] = exc.as_dict()
        write_report(report)
        return 2
    valid = True
    for name, spec in raw["adapters"].items():
        entry = {"valid": True, "error": None, "resolved": None}
        try:
            spec = validate_adapter(name, spec, raw.get("default_timeout_sec", 180))
            prefix = resolve_command(spec["command"])
            entry["resolved"] = prefix[-1] if prefix else None
        except DispatchError as exc:
            entry.update(valid=False, error=exc.as_dict())
            valid = False
        entry["available"] = entry["resolved"] is not None
        if entry["valid"] and entry["available"]:
            report["usable"] += 1
        report["adapters"][name] = entry
    report["valid"] = valid and bool(raw["adapters"])
    write_report(report)
    return 0 if report["valid"] and report["usable"] >= 1 else 2


def load_jobs(path):
    data = read_json(path, "JOBS")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) for j in jobs):
        raise DispatchError("INVALID_JOBS", "jobs file must contain a non-empty 'jobs' list of objects")
    ids = [j.get("seat_id") for j in jobs]
    if not all(isinstance(s, str) and s for s in ids) or len(set(ids)) != len(ids):
        raise DispatchError("INVALID_JOBS", "every job needs a unique non-empty 'seat_id'")
    base = Path(path).resolve().parent
    for job in jobs:  # relative prompt_file paths are relative to the jobs file
        if isinstance(job.get("prompt_file"), str) and not Path(job["prompt_file"]).is_absolute():
            job["prompt_file"] = str(base / job["prompt_file"])
    return jobs


def load_prompt(job):
    if isinstance(job.get("prompt_text"), str):
        return job["prompt_text"]
    if not isinstance(job.get("prompt_file"), str):
        raise DispatchError("INVALID_JOB", "job needs a string 'prompt_text' or 'prompt_file'")
    try:
        with open(job["prompt_file"], "r", encoding="utf-8-sig") as fh:
            return fh.read()
    except (OSError, ValueError) as exc:
        raise DispatchError("PROMPT_UNREADABLE", str(exc))


def run_jobs(jobs, adapters, workspace=None, keep_temp=False):
    before = workspace_snapshot(workspace)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda j: run_seat(j, adapters, keep_temp), jobs))
    after = workspace_snapshot(workspace) if before is not None else None
    mutated = before is not None and before != after
    total = len(results)
    ok = sum(1 for r in results if r["status"] == "ok")
    for r in results:
        if not keep_temp:
            r.pop("temp_dir", None)
    status = "ok" if ok == total else ("partial" if ok else "failed")
    report = {"status": status, "quorum": "%d/%d" % (ok, total), "succeeded": ok, "total": total,
              "workspace_mutation_detected": mutated,
              "workspace_guard": "active" if before is not None else "inactive",
              "results": results}
    if mutated:
        report["error"] = {"code": "WORKSPACE_MUTATION_DETECTED",
                           "message": "git status of the workspace changed during dispatch"}
    if mutated or any(r["status"] == "rejected" for r in results):
        code = 2
    else:
        code = 0 if ok == total else 1
    return report, code


def cmd_run(jobs_path, config_path, workspace, out_path, keep_temp):
    try:
        adapters = load_config(config_path)
        jobs = load_jobs(jobs_path)
    except DispatchError as exc:
        write_report({"status": "failed", "quorum": "0/0", "succeeded": 0, "total": 0,
                      "workspace_mutation_detected": False, "results": [], "error": exc.as_dict()}, out_path)
        return 2
    report, code = run_jobs(jobs, adapters, workspace, keep_temp)
    write_report(report, out_path)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Senato read-only multi-CLI seat dispatcher.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--doctor", action="store_true", help="validate config and adapter availability")
    mode.add_argument("--run", metavar="JOBS_JSON", help="run the jobs file")
    parser.add_argument("--config", help="adapter config path (else $SENATO_CONFIG, else ~/.config/senato/config.json, "
                             "else legacy ~/.config/council/config.json)")
    parser.add_argument("--workspace", help="git repo to guard against mutation")
    parser.add_argument("--out", help="also write the JSON report here")
    parser.add_argument("--keep-temp", action="store_true", help="keep per-seat temp dirs for debugging")
    ns = parser.parse_args(argv)
    if ns.doctor:
        return cmd_doctor(ns.config)
    return cmd_run(ns.run, ns.config, ns.workspace, ns.out, ns.keep_temp)


if __name__ == "__main__":
    sys.exit(main())
