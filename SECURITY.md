# Security

## Threat model

The other agent is untrusted by design. Two inputs cross that boundary; both are covered
in the README [Security](README.md#security) section:

- `evidence` fields are commands written by the other agent. The tool prints them and
  never executes them. Rule 4 requires the reader to inspect them first.
- `watch --exec` substitutes values from the other agent's outbox into a shell command.
  Values are `shlex`-quoted and ids validated against a fixed pattern; a POSIX shell is
  assumed.

Mailbox files are plain text inside the repository and may be committed.

## Reporting

Use GitHub's private vulnerability reporting on this repository ("Security" tab, "Report
a vulnerability"). Do not open a public issue for anything exploitable. Include the
`--exec` template or message record that demonstrates it.

Only the `main` branch is supported.
