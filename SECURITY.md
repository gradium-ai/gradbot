# Security Policy

## Reporting a vulnerability

If you believe you've found a security vulnerability in Gradbot, please report it privately rather than opening a public issue.

**Email:** security@gradium.ai

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof-of-concept if you have one.
- The version or commit you're testing against.

We aim to acknowledge reports within 3 business days. Once we've confirmed the issue, we'll work with you on a coordinated disclosure timeline.

## Supported versions

We provide security fixes for the latest released version on PyPI. Older versions are not maintained.

## Scope

In scope:

- The `gradbot` Rust core and its Python bindings (`gradbot_py`).
- The `gradbot_server` standalone WebSocket server.
- Authentication, credential handling, and audio/data flow within those components.

Out of scope:

- Issues in third-party LLM/STT/TTS providers used via configuration.
- Issues in the example demos under `demos/` (these are illustrative, not production code).
- Vulnerabilities that require physical access to a user's machine.
