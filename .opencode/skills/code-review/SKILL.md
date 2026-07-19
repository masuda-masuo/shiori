---
name: code-review
description: Review code changes and write findings as structured markdown suitable for GitHub issues. Use when reviewing diffs, commits, branches, or PRs. Produces a review in markdown format (not JSON).
---

# Code Review

Review code changes and produce findings in markdown format suitable for writing as a GitHub issue or PR comment.

## Review Modes

Two modes depending on the user's needs:

### Standard Review (default)

Balanced review focused on finding real bugs and design issues.

### Adversarial Review (`--adversarial`)

Actively try to find reasons the change should not ship. Question design decisions, assumptions, and edge cases.

## Determining What to Review

Based on input:

1. **No arguments**: Review all uncommitted changes
   - `git diff` for unstaged
   - `git diff --cached` for staged
   - `git status --short` for untracked files

2. **Commit hash**: Review that specific commit
   - `git show <hash>`

3. **Branch name**: Compare current branch to specified branch
   - `git diff <branch>...HEAD`

4. **PR URL/number**: Review the pull request
   - `gh pr view <number>`
   - `gh pr diff <number>`

## Gathering Context

Diffs alone are not enough. After getting the diff, read the full files being modified to understand context.

## What to Look For

### Bugs (primary focus)
- Logic errors, off-by-one, incorrect conditionals
- Missing guards, unreachable code paths
- Edge cases: null/empty, error conditions, race conditions
- Security: injection, auth bypass, data exposure
- Error handling: swallowed failures, unexpected throws

### Attack Surface (adversarial mode)
- Auth, permissions, tenant isolation, trust boundaries
- Data loss, corruption, duplication, irreversible state changes
- Rollback safety, retries, partial failure, idempotency
- Race conditions, ordering assumptions, stale state, re-entrancy
- Empty-state, null, timeout, degraded dependency behavior
- Version skew, schema drift, migration hazards
- Observability gaps

### Structure
- Does it follow existing patterns and conventions?
- Excessive nesting that could be flattened

### Performance
- O(n²) on unbounded data, N+1 queries, blocking I/O on hot paths

## Before Flagging Something

- Be certain. Investigate before calling something a bug
- Don't invent hypothetical problems — explain the realistic scenario
- Only review the changes, not pre-existing code
- Don't be a zealot about style

## Output Format (Markdown)

Write findings as markdown suitable for a GitHub issue.

```markdown
## Review: <summary>

### Finding 1: <title> (severity: high/medium/low)

**File:** `path/to/file.ts:42`
**Confidence:** 0.9

[description of the issue]

**Recommendation:**
[concrete change suggestion]
```

### Structure Rules
- Use `## Review:` heading with a one-line summary
- Each finding is `### Finding N:` with severity tag
- Include file path and line reference
- Confidence score 0.0–1.0
- Concrete recommendation for each finding
- Sort findings by severity
- Use `**Severity:** high/medium/low` for quick scanning
- For adversarial mode, start with a ship/no-ship assessment

### Quality Rules
- Prefer one strong finding over several weak ones
- Don't dilute serious issues with filler
- If nothing material found, say so directly with no findings
- Every finding must be grounded in the reviewed code or tool outputs
- Label inferences explicitly vs. observed facts
- Tone should be matter-of-fact and actionable, not accusatory
