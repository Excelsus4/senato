# Senato — Declarative External CLI Adapter Contract (`Tier 1`)

External CLIs change flags across versions and enforce read-only sandboxes differently across OSes. To prevent brittle vendor hardcoding and accidental workspace mutations, Senato defines a **declarative JSON adapter contract** loaded in priority order (`--config` > `$SENATO_CONFIG` > `~/.config/senato/config.json` > `~/.config/council/config.json`) and executed by `scripts/dispatch.py`.

## Schema

```json
{
  "version": 1,
  "default_timeout_sec": 180,
  "adapters": {
    "seat_adapter_id": {
      "command": "cli-binary-name",
      "args": ["--flag", "{prompt_file}"],
      "input_mode": "stdin | arg | file",
      "output_mode": "stdout | file",
      "timeout_sec": 180,
      "retry_on_stderr_regex": "optional-transient-startup-regex",
      "tested_version": "optional-version-string",
      "env_allowlist": ["OPTIONAL_VAR"],
      "extra_env": {"OPTIONAL_KEY": "value"}
    }
  }
}
```

### Field Semantics
- `command`: Executable name on `PATH` or absolute path. Executed via `subprocess.Popen` with `shell=False` (never via shell string interpolation or PowerShell piping).
- `args`: Array of string arguments. Supported safe single-pass placeholders:
  - `{prompt_file}`: Path to the seat's UTF-8 (No BOM) prompt file inside its isolated temporary directory (required when `input_mode == "file"`).
  - `{prompt_text}`: Raw UTF-8 prompt text passed as a single argv element (required when `input_mode == "arg"`; capped at 24 KB UTF-8 bytes to avoid OS argv limits — prefer `stdin` or `file` for larger packets).
  - `{output_file}`: Path to an isolated output file inside the temporary directory (required when `output_mode == "file"`).
  - `{temp_dir}`: Path to the seat's fresh empty temporary working directory.
- `input_mode`:
  - `stdin` *(preferred)*: Sends UTF-8 bytes directly on `stdin` with zero OS quoting hazards.
  - `file`: Writes UTF-8 prompt file inside `{temp_dir}` and injects `{prompt_file}` into `args`.
  - `arg`: Passes `{prompt_text}` in `args` (rejected above 24 KB UTF-8 bytes, and rejected for Windows `.cmd`/`.bat` shims).
- `output_mode`:
  - `stdout`: Captures UTF-8 `stdout` as the seat's response.
  - `file`: Reads `{output_file}` written inside `{temp_dir}`.
- `timeout_sec`: Hard wall-clock limit per seat. Exceeding it terminates the subprocess group/tree cleanly (Windows `taskkill /F /T`, POSIX `killpg`).
- `retry_on_stderr_regex` *(optional)*: Valid Python regex matching transient CLI startup errors (e.g., an empty session ID). When a seat exits non-zero and `stderr` matches, `dispatch.py` retries **at most once** in a fresh temporary directory (`attempts: 1 | 2`); never retries timeouts, empty outputs, or rejected flags.
- `env_allowlist` *(optional)*: List of extra environment variable names to forward alongside base OS paths when restricting the inherited environment.
- `extra_env` *(optional)*: Map of in-memory environment variables merged for the child process (never written to disk).

## Mandatory Read-Only & Security Guards (Enforced by `scripts/dispatch.py`)

1. **Physical CWD Isolation:** Every adapter subprocess runs with `cwd=<fresh_empty_temp_dir>`, completely outside the target repository.
2. **Known Write / Permission-Bypass Flag Rejection:** `scripts/dispatch.py` refuses to execute any adapter whose `args` contain known write-enabling or sandbox-bypass flags, including:
   - `--permission-mode` with `acceptEdits`, `bypassPermissions`, or `auto` (space or `=` form)
   - `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust`
   - `-s` / `--sandbox` with `workspace-write` or `danger-full-access` (including attached `-sworkspace-write` / `-sdanger-full-access` forms)
   - `-c` / `--config` setting `sandbox_mode` to `workspace-write` or `danger-full-access`
   - `--allowedTools` / `--allowed-tools` granting write or command-execution tools (`Edit`, `Write`, `Bash`, `PowerShell`, `NotebookEdit`, `MultiEdit`)
   - `--full-auto`, `--approve-for-me`, `--mode accept-edits`, `--mode auto`
   - *(Scope note: argv denylisting inspects explicit command-line flags only; vendor config files, named profiles such as `codex -p`, or third-party CLI flags outside this list remain the adapter author's responsibility to keep read-only.)*
3. **Target Workspace Git Mutation Guard:** When `--workspace <repo>` is supplied, `scripts/dispatch.py` snapshots both `git status --porcelain --untracked-files=all` and a SHA-256 hash of `git diff HEAD` before and after dispatch to detect tracked or untracked workspace mutations (note: `.gitignore`-ignored paths or paths outside the repository are outside git's visibility). Any detected change raises `WORKSPACE_MUTATION_DETECTED` and exits `2`.
4. **Honest Quorum Reporting:** If `k` of `N` seats succeed and `N - k` fail (timeout, missing binary, non-zero exit, empty output), `scripts/dispatch.py` reports `quorum: "k/N"` and exact per-seat error records — never fabricating substitute responses.
5. **Zero Secret Persistence:** Temporary prompt/output directories are removed cleanly in a `finally` block (unless `--keep-temp` is explicitly passed for debugging). Secrets and API keys are never written into workspace files.
