# Security Policy

## Supported versions

The latest release on the `main` branch receives security updates.

| Version | Supported |
|---|---|
| 0.1.x | :white_check_mark: |
| < 0.1 | :x: |

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, report them
privately using GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
feature (`Security` → `Report a vulnerability` on the repository page).

Include, where possible:

- a description of the issue and its impact;
- steps to reproduce or a proof of concept;
- affected version/commit;
- any suggested remediation.

You can expect an acknowledgement within a few business days. We will investigate,
keep you informed of the progress, and coordinate a disclosure timeline with you.

## Handling secrets

- Configuration is supplied through environment variables; `.env` is git-ignored.
- Committed example values in [`.env.example`](.env.example) are placeholders and
  must never contain real credentials.
- The `X-API-Key` header is validated with a constant-time comparison
  (`secrets.compare_digest`); production keys must be replaced before deployment.
- Container images are published to GHCR using the short-lived `GITHUB_TOKEN`,
  so no long-lived registry credentials are stored in the repository.
