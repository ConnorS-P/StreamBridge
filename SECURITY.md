# Security Policy & Architecture Invariants

## Core Security Invariants

1. **Zero Streaming Credential Handling**:
   - StreamBridge never prompts for, accepts, transmits, logs, or stores user streaming passwords, authentication cookies, or DRM keys.
   - Streaming authentication happens exclusively inside the official browser or an isolated `WebView2` profile (`%LOCALAPPDATA%\StreamBridge\BrowserProfile`) encrypted at rest via Windows DPAPI (`CryptProtectData`).
2. **Strict URL Allowlisting & Command Execution**:
   - All playback launch URLs must pass `validate_launch_url` (Python) and `ValidateHttpsNetflixUrl` (C++) enforcing `https://` and explicit provider hostname allowlists before reaching `CreateProcessW` or `subprocess.Popen(..., shell=False)`.
   - `shell=True`, `eval`, and `exec` are strictly forbidden and blocked by our CI `ruff` (`S` / Bandit) security gate.
3. **Kodi Repository Integrity**:
   - All published add-on archives in `repo/` are signed with SHA-256 checksums (`.zip.sha256`) enforced by `<hashes>sha256</hashes>` in `repository.streambridge/addon.xml`.
   - The `main` and `repo` branches and `v*` release tags are protected by GitHub Repository Rulesets preventing unauthorized force-pushes, deletions, or unverified modifications.

## Reporting a Vulnerability

Please report security issues privately via GitHub Security Advisories on [ConnorS-P/StreamBridge](https://github.com/ConnorS-P/StreamBridge/security/advisories).
