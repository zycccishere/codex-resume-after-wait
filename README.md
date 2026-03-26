# codex-resume-after-wait

Codex skill for handing off long blocking waits to a detached watcher and resuming the same session when the watched job exits.

## Install

```text
$skill-installer install https://github.com/zycccishere/codex-resume-after-wait/tree/main/skills/blocking-wait-handoff
```

Restart Codex after installation so the skill is discovered.

## What It Does

- hands a blocking wait off to an external watcher after a preflight window
- watches a local or remote PID, or a unique process pattern
- resumes the same Codex session with `codex exec resume`
- allows one active handoff per Codex session

## Notes

- Local `--pattern` mode expects `pgrep`.
- Remote monitoring expects `ssh`, `ps`, and `pgrep` on the remote host.
- Local PID watching is most reliable when Codex has full access or another mode that can inspect arbitrary local processes.
- In restricted sandboxes, local process inspection may fail even if the scheduler itself launches correctly.
- The resume path depends on the `codex` CLI being present on the same machine and supporting `codex exec resume`.

## Local smoke checks

```bash
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py --help
python3 skills/blocking-wait-handoff/scripts/codex_wait_handoff.py status --help
```
