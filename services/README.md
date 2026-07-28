# Scientific shims

`services/<name>/` packages (except `agent`) are backward-compatible shims.

Canonical code lives under `plugins/molmind_core/scientific/<name>/`.

Prefer the plugins path in new code; legacy services imports continue to work via shims.
