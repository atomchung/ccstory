# Project attribution strategy

Project attribution resolves an owner-initiated session to a durable canonical
project. It is a deterministic, inspectable layer between provider session
parsing and the existing project-to-goal mapping.

It does not infer progress, outcomes, or goal membership.

## Decision pipeline

```text
provider session
  -> owner-intent eligibility
  -> first real user input
  -> canonical project aliases
  -> accepted deterministic rules
  -> accept / conflict / abstain
  -> existing project-to-goal membership
```

## Profile vocabulary

- A **candidate project** is only a review suggestion derived from folder,
  repository, or workspace evidence. It is not a label and cannot train or
  accept a rule.
- A **suggested Project Profile** contains mined rule proposals. Suggested
  rules are inert.
- An **accepted Project Profile** is the current owner-approved configuration
  of active deterministic rules for known canonical projects. "Accepted"
  describes the rules' review status, not a claim that every session in the
  project is correct.

### 1. Establish owner intent

Project classification must not run on work that was not initiated as an
owner task. Exclude sessions when provider provenance identifies:

- a subagent or child conversation;
- a Claude-to-Codex or other agent delegation;
- scheduled automation;
- provider control messages rather than user input.

A local evaluator may also quarantine repeated prompt families and fixed-reply
health probes. Repetition is evaluated from local transcripts, but transcript
content is never exported to tracked fixtures.

### 2. Use intent evidence, not outcome summaries

The primary semantic evidence is the first real user input after provider
control payloads are removed. An AI-generated session summary describes what
happened later and can point at a different project, so it must not replace the
initial intent.

Provider-neutral metadata may include:

- workspace and repository identity;
- relative worktree or nested path;
- native title;
- the bounded first user input;
- explicit issue, artifact, or command identifiers when available.

Never infer a model or fabricate unavailable evidence.

### 3. Normalize canonical project identities

Directory names, worktrees, GTM folders, and historical repository names may
be aliases of one durable project. Normalize aliases before candidate
generation and scoring. Aliases must not compete as separate projects.

Alias mappings are owner-controlled local configuration. The sampler accepts a
private JSON mapping:

```json
{
  "example-app-gtm": "example-app",
  "legacy-example-app": "example-app"
}
```

Real mappings belong under the gitignored `.local-eval/` directory.

### 4. Apply inspectable rules

A Project Profile contains rules with:

- project ID;
- evidence field;
- matcher (`exact`, `prefix`, `glob`, or `token`);
- positive or negative polarity;
- authoritative or suggestive authority;
- weight, status, provenance, support, and measured precision.

Mined rules start as `suggested` and are inert. Only explicitly `accepted`
rules participate in normal evaluation. Every accepted result exposes the
matching rules and scores.

Authoritative negatives block a project. Multiple authoritative positives
produce a conflict. Weighted evidence must clear both a minimum score and a
winning margin.

### 5. Abstain safely

Evidence that is weak, contradictory, or outside known projects must not be
forced into the closest directory. The possible outcomes are:

- `accepted`: one project clears the policy;
- `conflict`: two or more projects remain plausible;
- `abstained`: evidence is insufficient or explicitly blocked.

Precision takes priority over coverage. Owner-reviewed rows with no project
are open-set negatives; accepting one is a false positive.

## Evaluation contract

Real evaluation artifacts remain local:

```bash
python scripts/project_attribution_sample.py \
  --days 60 \
  --sample-size 50 \
  --aliases .local-eval/issue-223/project-aliases.json \
  --exclude-evidence .local-eval/issue-223/scale-v2/discovery-labeled.jsonl \
  --exclude-evidence .local-eval/issue-223/scale-v2/formal-25-labeled.jsonl \
  --output-dir .local-eval/issue-223/pool
```

`--exclude-evidence` is repeatable and removes stable private evaluation IDs
before diversity sampling, so drift pools cannot silently reuse discovery or
formal cases.

Suggested rules are mined only from a discovery split:

```bash
python scripts/project_attribution_eval.py suggest \
  .local-eval/issue-223/discovery-labeled.jsonl \
  --split validation \
  --min-support 2 \
  --min-precision 0.95 \
  --output .local-eval/issue-223/profiles-suggested.toml
```

Before formal labels are inspected, freeze:

- the accepted profile hash;
- the formal evidence hash;
- the predictions and their hash.

Then evaluate the unchanged accepted profile:

```bash
python scripts/project_attribution_eval.py evaluate \
  .local-eval/issue-223/profiles-accepted.toml \
  .local-eval/issue-223/formal-labeled.jsonl \
  --split test
```

Report accepted precision, known-project coverage, unknown false-accept rate,
conflict rate, and abstention rate separately. Do not tune on the formal set.
Use new time-separated cases for later drift checks.

Accepted precision requires the complete predicted project set to equal the
owner label. A one-project prediction against a genuinely two-project label is
not counted as correct.

## Public synthetic stress tests

`tests/test_project_attribution_scenarios.py` exercises invented data only:

- folder, nested-repository, worktree, and alias candidate generation;
- accepted, conflicting, blocked, weak, and open-set rule decisions;
- first-owner-input extraction across Claude, Codex, and Antigravity;
- delegated and scheduled-session exclusion.

These tests verify the deterministic contract but do not replace
time-separated owner-labelled evaluation. They also preserve two boundaries
that need an explicit product decision before normal-report integration:

- candidate folder names are review suggestions, so incidental nested or
  worktree-derived names must not silently become accepted projects;
- a unique authoritative repository currently wins over contrary semantic
  evidence, so cross-project requests need dedicated evaluation coverage.

Single-field decisions also need their own held-out stratum. Requiring two
fields everywhere is not automatically safer: valid worktree and repository-
less sessions may expose only a canonical workspace. Normal-report integration
should remain paused until workspace-only decisions and cross-project intent
have been measured separately, rather than hidden inside an aggregate score.

## Privacy boundary

- `.local-eval/` is gitignored.
- Never commit real prompts, labels, absolute paths, private aliases, accepted
  local profiles, or frozen prediction files.
- Public tests use synthetic projects and transcripts.
- The deterministic engine and local evaluation make zero model calls.
- Normal recap and goal surfaces remain unchanged until profile loading is
  integrated as a separate product change.
