# Repository settings

What the GitHub repository is configured with, and why, so a fork or a fresh
repository can be set up the same way.

| Setting | Value | Why |
| --- | --- | --- |
| Merge methods | squash only, title and body from the PR | One commit per change on `main`, written once, in the PR |
| Delete branch on merge | on | Merged branches are noise |
| Wiki | off | Docs live in the repository, reviewed like code |
| Dependabot alerts and security updates | on | A vulnerable dependency opens a PR on its own |
| Dependabot version updates | `.github/dependabot.yml` | Pinned images and actions never update themselves |
| Ruleset "Protect main" | [`.github/rulesets/protect-main.json`](../.github/rulesets/protect-main.json) | See below |
| Secret scanning and push protection | on | GitHub's own scan, on top of the gitleaks job, and a push containing a known credential format is blocked |
| Private vulnerability reporting | on | What [`SECURITY.md`](../SECURITY.md) and the issue chooser's "Report a security vulnerability" link point at |
| Fork pull request workflows | approval required for first-time contributors | A first PR cannot run code in CI before a maintainer has looked at it |

The ruleset: no deletion or force-push, linear history, changes only through a
pull request (squash, review threads resolved), and the six CI jobs required:
`API`, `Web`, `Deploy`, `Lint`, `Images`, `Secret scan`. No approval count, since there is one
maintainer; the repository admin may bypass only through a pull request. The
CI workflows run on every push and PR with no path filter for this reason: a
required check that never runs blocks the PR forever.

```sh
R=kyler-frazier/open-worlds
gh api -X PATCH repos/$R -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F allow_squash_merge=true -F delete_branch_on_merge=true \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY -F has_wiki=false
gh api -X PUT repos/$R/vulnerability-alerts
gh api -X PUT repos/$R/automated-security-fixes
gh api -X POST repos/$R/rulesets --input .github/rulesets/protect-main.json
gh api -X PATCH repos/$R --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"},
                           "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
gh api -X PUT repos/$R/private-vulnerability-reporting
gh api -X PUT repos/$R/actions/permissions/fork-pr-contributor-approval -f approval_policy=first_time_contributors
```
