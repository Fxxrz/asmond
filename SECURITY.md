# Security Policy

## Local recording privacy

The optional `record` mode is explicitly started and writes private local session directories/files. It is not an anonymized support report: process basenames, PID/start identities, measurements and timing can reveal operating patterns. No process arguments, environment, user names, full executable paths, raw controller buffers or unassigned temperature debug keys are intentionally recorded. Review before sharing. No monitoring service, network listener, upload, automatic process action or login hook is installed. Only selected power telemetry uses sudo/powermetrics; recording as root is refused outside generated mock tests.

Daily files and the total size limit never remove old recordings. CSV export refuses existing output and escapes formula-like text, but remains sensitive local data; use trusted recordings. Malformed/incomplete rows are rejected, not silently repaired. Files are flushed per complete JSONL row, not guaranteed durable after a hard kill or power loss. An interrupted/failed export can leave a private partial destination; review it before manually removing it or retrying into a new directory.

## Supported versions

Security fixes are made against the latest released Asmond version. Older releases may receive a fix only when the change can be applied safely.

## Reporting a vulnerability

Please use GitHub's private security-advisory flow for the `Fxxrz/asmond` repository. Do not open a public issue for a vulnerability involving privilege boundaries, process termination, sensitive paths or raw Apple telemetry.

Include the Asmond version, macOS version, Mac model class, reproduction steps and the smallest safe diagnostic sample. `asmond report` is intended for support; `asmond probe --raw` is not anonymized and may contain identifiers or private paths.

The project is maintained on a best-effort basis. Receipt and next steps will be acknowledged when the report has been reviewed.

## Direct installer

`install.sh` is a normal-user installer and rejects unsupported operating systems,
root execution and Rosetta bootstrap sessions. It does not write Homebrew's Cellar
or silently take over existing commands. The optional Python.org fallback asks
permission separately and checks a pinned SHA-256 digest, the PSF package signer
and macOS installer assessment before invoking the system package installer with
administrator authorization. The Asmond installer itself remains unprivileged.

Asmond release archives/checksums must be assets of a published stable release
in `Fxxrz/asmond`, downloaded over HTTPS. Checksums detect mismatched or damaged
downloads; they are not an independent signature and do not protect against a
compromised repository/publisher. Executing a curl-delivered script likewise
requires trusting its source; inspect it first or use a reviewed tag URL.
Archives are bounded and allowlisted, without archive-driven filesystem
extraction, links or permission restoration. Candidate version/mock-report
checks run unprivileged with isolated temporary settings before activation.
Installed version directories are retained for explicit rollback. Ordinary
exit, interrupt and termination clean up owned temporary downloads; a hard kill
or power loss can leave temporary files or an installation lock for inspection.
