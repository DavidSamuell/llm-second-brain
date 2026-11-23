---
name: docs-capture
description: Record a durable finding from the current work in the team docs vault the moment it becomes durable - a decision, an architecture or interface clarified, a pipeline or runbook step worked out, an incident root cause, a team convention, an ML experiment result, dataset fact or training gotcha - and ingest it completely in this session. Use while working in a repo connected to the docs vault, or when asked to document, save or capture something in the vault.
---
# docs-capture

Keep the team docs current while you work. When a finding becomes durable, write it up as a
source and ingest it straight away with the `docs-ingest` skill: submit, claim, source summary,
human docs, wiki and graph, index and log, complete. Don't batch findings for the end of the
session, and never leave them in `raw/` for a later run.

## What to capture

Capture, once each is settled:

- a decision made (what, why, alternatives rejected, who decided);
- an architecture, interface, schema or configuration clarified;
- a pipeline or runbook step worked out (commands, inputs, outputs, where it runs);
- an incident's confirmed root cause and fix;
- a team convention agreed.

### ML and research work

In repos that train, evaluate or ship models, also capture, once confirmed:

- dataset versions and provenance (manifest or hash, source corpora), train/dev/test split
  definitions (held-out speakers, age bands, domains), and text normalisation or label-set rules
  (e.g. a phone set mapping), since changing any of them silently changes the metrics;
- final evaluation results for a candidate model: model and checkpoint, data version, code commit,
  test set, decoding or inference parameters, headline metrics, and the experiment-tracker run
  link (e.g. MLflow run id). Summarise the headline numbers; link the tracker for the rest;
- the evaluation protocol and the current baseline or best model per test set;
- negative results and abandoned approaches, with the evidence (what was tried, on what data,
  what it scored against the baseline), so nobody reruns a dead end;
- training gotchas: divergence or numerical issues and their fix, hardware and memory limits
  (batch size that fits, time and cost per epoch), environment or dependency pins that matter;
- error-analysis conclusions that change data collection or modelling plans;
- model lineage (which checkpoint ships, and the data, code and recipe that produced it),
  promotion criteria, and accuracy lost to export or quantisation (e.g. ONNX, int8).

Put each where its readers look: a result that settles a choice goes in the decision record;
the eval protocol, baselines and training gotchas go in the pipeline doc; release lineage and
promotion criteria go in the project page or a runbook. A run often finishes after the session
that started it: when its final results reach a later session, capture them then.

A hypothesis the team is actively testing may be recorded only under `## Open questions` on the
wiki page of the component it concerns, marked unconfirmed, never in a human doc as fact.

Never capture: transient debugging, guesses or unconfirmed hypotheses (except as an open question,
above), personal data, secrets, credentials, `.env` contents, raw logs, per-step training logs or
full metric dumps, large code, or facts the vault already states. If the person asks you not to
capture, stop for this session.

## Steps (vault-mcp)

1. Once per session: `get_schema`, and `project_context(repo=<repo id>)` for the repo you work
   in. The repo id is `git remote get-url origin` (any form; the server normalises it, e.g.
   `github.com/org/app`). No project returned: offer once to create one from the repo's README
   and code, and continue if the person agrees or doesn't object. Ask which domain it belongs to
   when that isn't obvious from the existing `domains/`; create a new domain only if the person
   agrees (`docs-ingest` step 7).
2. **Dedupe:** `search` (two phrasings) and `graph_neighbors` around the project or component.
   Already stated the same way: stop. Stated differently: this is an update; capture what changed
   and what it replaces.
3. **Write the source** as short factual notes, one finding per source:
   - repo facts cite `repo@commit:path` with the commit from `git rev-parse HEAD`
     (e.g. `github.com/org/app@3f2c9e1:train/config.yaml`). Cite committed code only; if a
     finding depends on uncommitted changes, capture it after they are committed;
   - decisions from the conversation say who decided and when;
   - web content: fetch it with your own tools and include the content, not just the URL.
4. `submit_source(title, origin, content)` with origin `repo:<repo id>@<commit>` for repo
   reading, or `capture:<repo id>@<commit>` for a session finding. Keep the returned `raw/…`
   path.
5. Follow the `docs-ingest` skill from its claim step with that path, to the end
   (`complete_source`). The human docs it updates get a change-log line and citations. Every
   human doc you create or change, including a one-line decision record, also gets a wiki page
   (the component or concept it concerns; create it if missing) that states the current fact and
   carries a `documented_in` edge to that doc with source and evidence. Don't skip this for small
   findings.
6. Tell the person in one line what you saved and where (source summary and docs touched).

If a step fails, `docs-ingest` releases the source (`release_source`); tell the person which
pages were already written and that the source is back in `raw/` for another agent. Don't claim
a rollback.

## Local clone

Same steps; write the source file into `raw/` yourself, then follow the `docs-ingest` local-clone
steps and push.
