# Security Policy

## Supported Versions

ROM Audit Tool is under active development. Security fixes are applied to the latest release only.

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |
| Older   | :x:                |

## Reporting a Vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities.

Instead, report privately via one of the following:

- **GitHub Security Advisories**: use the [Report a vulnerability](https://github.com/muckypaws/rom-audit-tool/security/advisories/new) button on this repo's Security tab (preferred — keeps the report private until a fix is ready)
- **Email**: [your-security-contact-email] — please include "SECURITY" in the subject line

### What to include

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal example, if possible)
- Which platform(s) it affects (Batocera / RetroPie / Recalbox / all)
- Your assessment of severity, if you have one

### What to expect

- **Acknowledgement** within 5 working days
- I'll investigate and keep you updated on progress at least every 2 weeks until resolved
- If confirmed, a fix will be prioritized and a GitHub Security Advisory published once a patched version is available, crediting you (unless you prefer to stay anonymous)
- If declined (not reproducible, out of scope, or not deemed a security issue), I'll explain the reasoning

### Scope

This is a ROM auditing and launch-verification tool. Relevant areas of concern include:

- Command/path injection via crafted ROM filenames or metadata
- Unsafe handling of screenshot/log file paths
- Any code execution triggered by processing untrusted ROM sets or config files

Vulnerabilities in the emulators, platforms (Batocera/RetroPie/Recalbox), or third-party dependencies themselves should be reported to those projects directly, not here.

Thanks for helping keep this project and its users safe.
