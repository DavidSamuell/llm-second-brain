# Spec: write-policy

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Approved; implementation awaiting Checkpoint I review · **Depends on:** —

## Objective

Agents keep the whole team documentation current, not only `wiki/`. Today `vault-mcp` lets agents write only in `wiki/`, `raw/`, `_system/` and on pages marked `maintainer: agent|shared`; a `maintainer: human` page is locked for good. In v0.2:

- Agents may create and edit the human-facing pages: `domains/` (domain and project folders) and `shared/`. Schema 2 and 3 used flat `projects/`, `specs/`, `pipelines/` and `runbooks/` folders instead.
- People usually edit through an agent. When a person edits a page by hand, the page stays editable by agents, so obsolete information can still be corrected later.
- A short list of files that define the rules stays locked against agents, whatever their frontmatter says.
- Once the watcher records a web save, agents wait through the configured grace period. This reduces concurrent-edit risk; it cannot protect unsaved keystrokes or saves the watcher has not recorded yet.

**Users:** team members (through their agents), the nightly maintenance agent, and admins who set the policy.

## Behaviour

### Policy (`vault-mcp.yaml`)

```yaml
write:
  allow: [wiki/, raw/, _system/, domains/, shared/]
  human_pages: edit_with_notice    # new-deployment example; omitted field keeps v0.1 read_only
  protected: [AGENTS.md, CLAUDE.md, skills/, _system/templates/, _system/graph/ontology.yaml]
  human_edit_grace_s: 90           # refuse agent writes this soon after a web-editor save
  delete: false
  max_file_mb: 10
```

### Decision order for an agent write

1. Readable (`read.deny`, hidden paths, credential `--folders`): unchanged from v0.1.
2. Under `write.protected` → refused `forbidden`, reason `protected`. Applies to `write`, `append`, section tools and `move` (source and target).
3. The most recent history version with `source: web` is less than `human_edit_grace_s` old → refused `recently_edited` with `details.retry_after_s`. Later MCP or scan versions must not hide that save. Nothing is written.
4. `maintainer: agent|shared` → allowed anywhere readable.
5. `maintainer: human`:
   - `read_only` → refused `forbidden`, reason `human_page`, as in v0.1;
   - `edit_with_notice` → allowed. The audit line carries `human_page: true`, and one review-queue notice per page per day is added: `- [ ] … [[page]] — Agent updated a person-maintained page — <the agent's change summary> — by <credential> #review/agent-edit`. The write is not delayed or held for approval.
6. No `maintainer` → allowed only under `write.allow`.

The write tools gain an optional `summary` argument (≤ 200 characters). It is used in the notice and in the audit line, and is required when the target is a `maintainer: human` page (`invalid` without it).

For version-checked writes, the writer validates permissions and the required summary, then checks `base_version` before checking grace. A stale base therefore remains `conflict`, independent of watcher debounce timing; a current base inside the recorded web-save window gets `recently_edited`. Grace still blocks every otherwise-permitted write and move.

### `maintainer` values (AGENTS.md, schema_version 2)

| Value | Meaning |
|---|---|
| `shared` | The default for pages in the human-facing folders. People and agents both edit; agents keep it current. |
| `agent` | Agent-generated reference (`wiki/`). People may still edit. |
| `human` | Written by a person. Agents may update it when information is obsolete or contradicted by a newer cited source, must cite that source, and keep the person's structure and wording elsewhere. Each such edit raises a review notice. |

### Upgrades

- Kit default for new deployments: the policy above.
- Existing deployments keep their `vault-mcp.yaml` (setup never overwrites it). UPGRADING.md gives the new block. `scripts/upgrade-vault` brings AGENTS.md to the current schema_version.
- Schema 4 changes the layout to `domains/` and `shared/`. An existing policy that lists the old folders needs `domains/` and `shared/` added to `write.allow`, and pages in the old folders must be moved by hand with the `move` tool.
- Bookbot: apply the new policy in this module's rollout.

## Commands

```bash
uv run pytest vault-core vault-mcp -q -k "policy or write"      # unit
uv run pytest e2e -k "human_page or protected or recently_edited" -m "profile_local or profile_none"
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Project structure

- `vault-core/src/vault_core/policy.py`: `WritePolicy` fields and `check_write` order above.
- `vault-mcp/src/vault_mcp/writer.py`: grace check (finds the most recent web version), notice, audit field, `summary` argument.
- `vault-core/src/vault_core/review_queue.py`: notice format (`#review/agent-edit`, one per page per day).
- `vault-mcp.example.yaml`, `vault-template/AGENTS.md` (§3), `docs/USER-GUIDE.md`, `docs/CONNECT-AGENTS.md`, `docs/SECURITY.md`, `docs/UPGRADING.md`.

## Code style

Follow the existing pure decision functions: a reason literal per refusal, no I/O inside policy.

```python
def check_write(
    path: str,
    policy: Policy,
    folders: list[str] | None,
    *,
    exists: bool,
    maintainer: str | None,
    web_saved_s_ago: float | None,
) -> Decision:
    """v0.2: protected paths never; a fresh web save waits; human pages per `human_pages`."""
    readable = check_read(path, policy, folders)
    if not readable.allowed:
        return readable
    if under(path, policy.write.protected):
        return Decision(False, "protected")
    if web_saved_s_ago is not None and web_saved_s_ago < policy.write.human_edit_grace_s:
        return Decision(False, "recently_edited")
    ...
```

## Testing strategy

- **Unit (vault-core):** a table test of `check_write` over every row of the decision order, both `human_pages` modes, and `protected` matching for files and folders.
- **Unit (vault-mcp):** recent and expired web-save timestamps; notice deduplication per page per day; `summary` required on human pages; audit field present.
- **e2e (local and none profiles):** acceptance 1–6 below against the real stack, with web saves simulated by filesystem writes, as in §16.14.
- Update the v0.1 tests that assert `human_page` refusal to run in `read_only` mode, which still behaves the same.

## Boundaries

- **Always:** keep history, audit and the secret scan on every agent write; keep `delete: false`.
- **Ask first:** adding paths to the default `protected` list, or changing the default grace.
- **Never:** let an agent edit `AGENTS.md`, `CLAUDE.md`, `skills/` or the ontology through MCP. These define the rules; an injected source must not be able to rewrite them.

## Success criteria

1. A write token creates `domains/asr/parakeet/README.md` (no maintainer) and `domains/asr/parakeet/pipelines/etl.md`; both succeed, with history and audit.
2. In `edit_with_notice` mode an agent edit of a `maintainer: human` page with a `summary` succeeds. The audit line has `human_page: true`, and exactly one `#review/agent-edit` notice appears even after three edits that day. Without `summary` → `invalid`.
3. In `read_only` mode the same edit is refused `human_page` (v0.1 behaviour kept).
4. `write` to `AGENTS.md` and `move` of a page onto `skills/x.md` are refused `protected`.
5. A web save followed by an agent write within 90 s → `recently_edited` with `retry_after_s`, and the file still holds the person's text; the same write after the grace succeeds.
6. All v0.1 acceptance tests still pass.

## Open questions

- None blocking. The 90 s default comes from Ignis autosaving every few seconds, plus a margin for someone still typing.
