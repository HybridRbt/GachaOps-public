# Compatibility evidence

The machine-readable source is `gachaops/assets/support_matrix.json`; run `python scripts/check_support_matrix.py` to validate IDs, modes, dates and evidence references. This display does not alter adapter admission rules, native hashes or configuration checks.

| Adapter | Verified version / contract | Start modes | Current public evidence level |
|---|---|---|---|
| OK-WW | Saved DailyTask / installed PyAppify metadata contract | Assistant, manual | Offline verified; version-specific Windows evidence not promoted |
| ZZZ-OneDragon | Saved preset / owned structured callbacks | Assistant | Offline verified; historical live six-game result is separate |
| MaaEnd | v2.34.0 LaunchGamePC; legacy direct-entry contract | Assistant, manual | Offline verified; Global/Auto default-location launch lifecycle awaits live acceptance |
| MaaGF2 | v2.7.3 / MFAAvalonia v2.16.2, actual MFAAvalonia entry and verified Core hashes | Assistant | Offline verified; v2.8.0 / MXU unsupported, including stale MFA files; unknown entry identity blocked |
| OK-NTE | Installed CLI selector / official-launcher metadata contract | Assistant | Offline verified; version-specific Windows evidence not promoted |
| March7 | v2026.9.30, v2026.10.16 | Assistant | Offline verified; new-version natural daily acceptance remains separate |
| BetterGI | 0.66.0.0 | Assistant | Offline verified; HYB-66 live single/serial acceptance pending |

All seven have implementation and no-game regression coverage. None is newly declared `windows_verified` by this milestone. A future promotion needs exact versions, a dated scenario and evidence; old six-game acceptance does not cover current updates or BetterGI. Unsupported native versions/protocols retain their existing adapter blockers. Adapters that validate contracts rather than a fixed version whitelist remain described as contract-based; this matrix does not invent a whitelist.

Preparation now fingerprints installation-owned executables and selected version/configuration metadata. The execution entry rechecks those fingerprints before creating a Windows process; drift blocks the chain and asks for a new check. March7's child-launch version/config hash guards and exact-SHA trial guards remain in place. This closes the preparation-to-launch drift window; it does not claim updates occurring after launch can be safely accepted.

Known limits: OK-WW manual entry avoids one observed Fatal startup route, without establishing its root cause. MaaEnd native launch supports explicit Global/Auto and its verified installation layout; custom layouts and ambiguous launcher paths remain blocked. MaaGF2's source/account setup can fail inside the native assistant; GachaOps records that failure and never fills account type or credentials. Critical/unknown damaged log callbacks do not become successful simply because the executable exits.
