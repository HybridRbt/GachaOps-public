# Contributing to GachaOps

Work from the current reviewed engineering baseline in an isolated worktree. Keep user installations, account information and native task presets outside the repository. Project code uses AGPL-3.0-only; preserve third-party licenses and contribution rights.

Start with `python -m unittest discover -s tests -v`. Optional UI tests require the pinned requirements-desktop.txt and must be reported as run or skipped. Use UTF-8 mode on Windows and a writable temporary directory outside the source tree; a temporary frozen app inside the source tree can discover its parent source deployment. Keep fixture evidence and live game acceptance separate.

Behavior changes need a failing regression before implementation, followed by targeted and full tests. Include the exact command, platform, test counts/skips, risks and evidence in the PR. Documentation and visual changes need relevant link/layout checks rather than tests that grep prose. Update the development state and compatibility record when relevant. New adapters follow [the adapter guide](docs/ADDING_ADAPTER.md).

Preserve the global execution lock, durable shared journal, Windows Job ownership and borrowed-game distinction. An exit code or disappearing window alone is not completion evidence. Cleanup uncertainty stops the chain. Native presets are user-managed; no speculative settings edits, account-type guessing, arbitrary argv plugins or process-name killing. Retries remain an explicit existing user setting, default zero; new retry/fallback policies require separate design and authorization. Never run a real assistant/game as an implicit test.

Use only synthetic test configurations/logs. Preview a sanitized diagnostic bundle before attaching anything; never attach raw native logs, account screenshots, local bindings or credentials. Run the public-artifact and support-matrix checks for a proposed public source export. Keep private history intact; no force pushes, visibility changes, main merges or releases without the corresponding maintainer authorization.

Please keep reviews focused and evidence-based. Describe actual versus expected behavior, acknowledge unresolved observations, and avoid personal attacks. Report potential security issues using SECURITY.md rather than publishing sensitive reproductions.
