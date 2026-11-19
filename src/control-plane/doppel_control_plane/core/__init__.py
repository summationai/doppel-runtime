"""Pure decisions: data in, data out. No clock, no database, no network.

    commands   parse `@doppelwerk <command> [reviewers...] [description]`
    payer      whose sign-in pays for a reviewer
    matching   whether a queued slot can start now, and if not, why
    merger     the PR's one approval decision, and its status comment

Imports only `doppel_shared.types`.
"""
