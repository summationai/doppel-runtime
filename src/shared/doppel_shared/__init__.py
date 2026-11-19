"""Contracts between the doppel runtime's components. Imports nothing else of ours.

types      plain data: PRs, reviewers, slots, verdicts, merge decisions
ports      interfaces one component offers another (Protocols)
capability how a run's capability is hashed, by whoever mints or checks one
testing    in-memory fakes of the ports, for any component's tests
"""
