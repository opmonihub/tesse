# Canonical system namespace

There is no strategy-specific executable kernel in this package. Definitions
and system identities live in the [catalog](../../catalog/README.md), resolved
by `system_id` and `system_version`. Reusable behavior belongs to registered
primitives and adapters. The [operational runtime](../../docs/runtime-authority.md)
resolves promoted models and verified providers, not Python paths to strategies.
