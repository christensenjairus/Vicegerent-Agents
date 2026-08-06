# Managed Homebrew packages

`packages.json` is the desired state for host tools whose CLI behavior is part of the platform. Repository-managed entries name immutable, version-qualified formulae under the root `Formula/` directory. `terminal-notifier` deliberately uses Homebrew Core so Homebrew can install its supported bottle instead of requiring full Xcode; the reconciler still verifies its exact version, binary ownership, and pin. The repository is tapped from GitLab as `vicegerent/packages`, so no separate tap repository is required; before applying, the managed tap checkout is synchronized to the exact commit of the invoking Vicegerent checkout, including a pre-merge task branch.

```bash
./vicegerent host-packages check
./vicegerent host-packages apply
```

`check` is read-only and fails when a required formula is absent, the linked binary belongs to another formula, the version probe does not report the declared version, or the formula is not pinned. `apply` installs and probes the desired keg before changing links, then unlinks and uninstalls only each package's declared floating replacements, links the exact formula without overwriting unrelated links, and pins it. The confirmation lists those removals; `--yes` accepts them non-interactively.

A repository-formula upgrade is a repository change: Renovate updates the desired version in `packages.json`, then the merge-request pipeline runs `host/brew/generate.py`, downloads the declared immutable release artifacts, calculates their SHA-256 checksums, renders a new versioned formula from `host/brew/templates/`, updates the manifest's formula reference, and commits those generated files back to the Renovate branch. Old versioned formulae are never edited or deleted, which preserves rollback. Homebrew Core entries are not generated; their declared version must be available from Homebrew Core. A non-recursive follow-up pipeline validates the generated branch head. Homebrew still owns transitive dependencies, so this contract pins direct host tools rather than claiming a byte-for-byte dependency closure.

Each manifest entry declares its upstream release source and generator strategy. New releases appear in Renovate's dependency dashboard and require explicit approval before Renovate opens an MR. CI owns repository formula and checksum generation; the remaining human gate is installing the package on a supported Mac and verifying `./vicegerent host-packages check` and `./vicegerent mcp doctor` before merge. If upstream packaging or build instructions for a repository formula change, update the corresponding checked-in template in the same MR rather than editing generated formulae.
