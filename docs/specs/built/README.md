# Built: what the runtime does today

One file per core feature. Each one records what is built, what it supports, and the decisions locked into it. Each locked decision names the test that holds it in place.

| File | Feature |
| --- | --- |
| [runtime.md](runtime.md) | The process: listeners, roles, config, restarts, the ledger |
| [intake.md](intake.md) | Moved to Doppelwerk: finding work on GitHub |
| [scheduler.md](scheduler.md) | Moved to Doppelwerk: slots, waits, retries |
| [merger.md](merger.md) | Moved to Doppelwerk: the one approval per PR |
| [commands.md](commands.md) | Moved to Doppelwerk: `@doppelwerk …` in PR comments |
| [credentials.md](credentials.md) | Codex sign-ins (the broker), capabilities and the model route (the gateway) |
| [sandbox.md](sandbox.md) | The pod per PR, reviewers inside it, mirrors and checkouts |
| [github.md](github.md) | Moved to Doppelwerk: the GitHub App |

## How these relate to the other specs

- [doppel-service.md](../doppel-service.md) is the design: why, and what we intend. It can run ahead of the code.
- [checkpoints.md](../checkpoints.md) is the build plan and progress.
- `built/` is what the code does now. Where it and the design disagree, `built/` is right about today and the design is right about intent. A gap belongs in a file's **Not yet** section.

## Rules

- **Each file has the same shape:** what it is, **Supports**, **Locked decisions**, **Not yet**.
- **A locked decision** is a behaviour someone chose: a number, a rule, a refusal. Changing one is a decision too. The PR that changes it updates the row (value, why, since) and the test named in **Proven by**, and says who decided.
- **Update in the same PR.** A PR that changes behaviour described here updates the file.
- **Proven by** names a test that fails if the decision is broken. A row with no such test says so ("not tested").
- Code links are relative to this folder; tests are named `file::test`.
