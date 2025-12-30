# Security Policy

This tool is designed to export local repositories to text endpoints for LLM reading.
It is easy to accidentally expose secrets.

## Do NOT expose secrets
Before sharing any tunnel URL:
- Remove or relocate `.env` and other secret files outside the repository.
- Make sure no private keys/certificates exist in the repo.
- Review `/tree` output to ensure ignored rules work as expected.

## Reporting
If you find a security issue, please contact the maintainers privately.