"""Acceptance tooling (phase 10): eval set, load test, preflight.

Everything here is owner-run (`make eval`, `make load-test`,
`make preflight`) and never part of the request path. The offline
variants run against a throwaway database created next to the real one
and torn down afterwards; the live eval is the only thing that spends
money, and it says so before it starts.
"""
