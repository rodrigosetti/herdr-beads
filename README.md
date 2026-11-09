# Beads in Herdr

User-level native Herdr mods for browsing any project's Beads issues and opening issue-specific omp worktree sessions. This standalone source checkout lives at `~/.local/share/herdr-beads`, outside project repositories. Shipped Herdr layouts and mods are not modified.

## Install and open

The dashboard/legacy launcher requires Herdr running locally, `hc`, `bd`, `git`, `omp`, and Python 3 on PATH. The independent background service has narrower requirements, documented below. Node is only needed for the model regression tests.

```sh
bash ~/.local/share/herdr-beads/install.sh
cd /path/to/project
go-bd --dashboard
```

The installer retains the existing mods, personal layout, and legacy launcher setup, and also provisions the per-user launchd service described below. It copies `beads-dashboard`, `bead-detail`, and `beads-sidebar` into `${HERDR_APP_HOME:-~/.config/herdr-app}/mods`, installs the personal `beads-workspace` layout, and updates `~/.local/bin/go-bd` together with its `go-bd-session.mjs` startup extension and `beads_epics.py` policy module. Keep these three files together. An existing personal layout file is preserved on reinstall. Keep `~/.local/bin` on PATH. `HERDR_APP_HOME` selects a different Herdr window/configuration.

`go-bd --dashboard` opens or focuses the project's Beads tab in its main-checkout space. The dashboard stays pinned to that project when another pane changes directory. Invoking the command from a linked worktree or nested folder resolves the same main checkout and database.

### Sidebar button

Switch to the personal layout once:

```sh
herdr-app do layout.switch --name beads-workspace
```

Click **Beads** above Settings in the sidebar. It opens a dashboard tab inside the currently selected space, using that space's first terminal's live folder. If the space already has a Beads dashboard, the button selects its existing tab and focuses the dashboard pane instead of creating a duplicate. The button remains available as an icon when the sidebar is collapsed and highlights while its dashboard tab is selected.

Unlike the sidebar button, `go-bd --dashboard` opens the dashboard in the project's main-checkout space. Both views use the same shared project database. Herdr remembers the selected layout across restarts; switching back to `main` restores the shipped interface without removing any tabs or sessions.


## Background lifecycle service (macOS)

The single per-user LaunchAgent `com.herdr.beads` starts at login and stays supervised independently of Herdr windows. Installation requires `git`, `bd`, and Python 3 with `venv`/`pip`. The dedicated service venv installs `requirements.txt` (`jsonschema` and `referencing`); the installer records absolute executable paths and never relies on an interactive shell's PATH. Missing optional `gh`, `omp`, `hc`, or `herdr-app` is reported explicitly and does not prevent unrelated bookkeeping. Installing the service does not launch or stop the Herdr app.

Reconciliation runs at startup and every **30 seconds**: a modest idle cadence that notices local ref/history changes and missed events without a busy loop. Validated successful mutating socket requests wake it immediately; `SIGUSR1` and `SIGCONT` also request a fresh pass. After OS resume it reconciles on wake or the next poll. Events are hints, never merge evidence. Pending/uncertain execution cannot block independent merge observation. Unchanged observations refresh service health without accumulating durable operations/audit entries on every tick.

Local-target observation uses the recorded canonical repository/full ref offline, independently of GitHub. Missing provider access holds only its PR/remote-target run; PR evidence is never downgraded to local proof. Missing execution/workspace capabilities refuse launch/resume/focus, not already-submitted Git/Beads bookkeeping. Herdr-unavailable hosting errors stay failures; no synthetic successful launch or alternate harness is supplied. The scheduler neither parses omp internals nor requires an omp process.

Register each canonical main checkout explicitly, including projects with no running session; repeat `--project` for additional projects. Linked-worktree or symlink aliases are refused. Reinstallation merges registrations, preserves the existing machine identity, run store, health file, and logs, and refreshes code and executable paths. It never closes Beads issues or deletes workspaces.

```sh
bash ~/.local/share/herdr-beads/install.sh install --project /absolute/main-checkout --project "/absolute/another checkout"
bash ~/.local/share/herdr-beads/install.sh status
bash ~/.local/share/herdr-beads/install.sh restart
bash ~/.local/share/herdr-beads/install.sh stop
# Provision/reinstall without loading, unloading, or querying any live launchd job:
bash ~/.local/share/herdr-beads/install.sh install --no-start --project /absolute/main-checkout
```

`stop` unloads only this user's service and retains its configuration/state; `restart` loads it again. Normal install and restart use scoped `launchctl bootout`, `bootstrap`, `kickstart`, and `print` in `gui/$(id -u)`. Failures are nonzero with diagnostics; only bootout's explicit “No such process”/missing-service result is accepted as already stopped. Startup must report service health within ten seconds. `--no-start` makes **no launchctl calls**, even when reinstalling over a currently loaded job.

Defaults:

- Installed Python sources, schemas, and venv: `~/.local/share/herdr-beads-service` (outside repositories).
- Config: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/service.json`.
- Runtime health: the same directory's `service-status.json`; persistent store: `store`; socket: `controller.sock`.
- Logs: the same directory's `logs/stdout.log` and `logs/stderr.log`.
- LaunchAgent: `~/Library/LaunchAgents/com.herdr.beads.plist`.
- Stable existing-or-created machine identity: `${XDG_STATE_HOME:-~/.local/state}/go-bd/machine-id`; an invalid identity is refused, never replaced.

Service/config/store/log directories are owner-only; config, plist, and logs are 0600. The plist contains only explicit `HOME`, `USER`, `LOGNAME`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `HERDR_APP_HOME`, and a bounded executable-directory/system PATH; inherited `BEADS_DIR` and secrets are excluded. `status` prints config, health, log, socket, and store paths, optional capability gaps, launchctl state, and runtime health; launchctl or health errors remain visible and return nonzero. For direct launchd diagnostics:

```sh
/bin/launchctl print "gui/$(id -u)/com.herdr.beads"
```

For isolated provisioning, set absolute `HERDR_BEADS_INSTALL_ROOT`, `HERDR_BEADS_STATE_HOME` (the state-home base), and `HERDR_BEADS_LAUNCH_AGENTS`; use the same overrides for subsequent management. These do not create a second label. **Use `--no-start` for isolation:** without it, management still targets the one normal per-user label. `HERDR_BEADS_PYTHON=/absolute/python3` selects the bootstrap interpreter; the venv uses its resolved real interpreter, not a pyenv shim.

The installed package includes the repository's Python adapters and `.mjs` extension files together. Composition is static: only `beads_omp.create_adapters(executables)` may supply execution/workspace adapters, with the service's exact socket bound to the concrete execution adapter. Missing module/factory means unsupported execution capabilities, not a legacy fallback; concrete adapter delivery is `herdr-beads-2hv.3`.

Installed smoke evidence: a disposable real Beads/Git project with no remotes reached the persisted manual handoff with all four optional tools disabled; singleton refusal, canonical-target invalidation, periodic rediscovery, process suspend/resume, SIGKILL/launchd recovery, idempotent reinstall/stop, externally closed/released workspace retention, and read-only sessionless epic progress were exercised. A second installed PR run remained visibly held for missing `gh` while local work progressed. Execution receipts were seeded through the deterministic test adapter; real omp launch and managed-project PR/UI cutover are not inferred. The active user Herdr app was not quit, and machine-wide sleep was not exercised.

## Dashboard

- Lists all non-closed issues, including deferred issues, without the CLI's default 50-row limit.
- Uses actual parent IDs for epic/subtask nesting. Epics and sibling issues sort by priority, then ID. Closed ancestors are fetched as structural context where needed.
- Displays issue ID, title, priority, type, durable Beads status, dependency blocking, and associated live sessions. Issues returned by `bd ready --limit 0` show **Ready** in green instead of Open; an unblocked in-progress or deferred issue is not automatically ready.
- Click an issue to open its details on the right. Further selections reuse the same pane. Details have a title/status header, compact metadata and label pills, Markdown narrative sections, and relationship/comment cards. Remaining nonempty `bd show --long` fields stay available under Additional details. Dates are localized with original timestamps in tooltips; Beads' zero timestamps show Not recorded. Ready badges also apply to related issues. Refresh reloads issue data and readiness.
- The left side is the **issue list pane**; the right side is the **issue detail pane** (a master–detail layout). Click a parent or relationship ID in the detail pane to open that bead and highlight/scroll to it in the list. Navigation expands its ancestors and shows filtered-out or closed targets as uncounted Navigation context without changing the active filter.
- **Back** and **Forward** in the detail toolbar revisit selections from either pane. Selecting a different bead after going Back discards the forward branch; selecting the current bead adds no history entry. History lasts for the current detail-pane instance and resets when it closes or reloads.
- **All open** (the initial view) includes every non-closed issue, including blocked, in-progress, and deferred work. **Ready** shows the union of authoritative `bd ready` matches and all `in_progress` issues. In-progress issues keep their In progress status labels. Both buttons show counts without duplicates; ancestor/navigation context does not inflate them. Closed issues stay hidden except when needed as ancestor or selected navigation context.
- Click a disclosure arrow to fold children. Folding and the selected All open / Ready filter are saved per machine/database. The old blocked-filter preference is ignored. There is no search field.
- Epic rows show closed/total **direct children**, including closed children hidden from the list. Ready filtering and folding do not change those counts.
- Beads queries refresh every ten seconds or via Refresh. A query failure is visible and does not replace the last good snapshot with an empty list. Epic-reconciliation failures appear separately and do not prevent issue queries, including the initial load; previously fetched epic progress remains available until reconciliation succeeds.

The mod uses Herdr's `exec` API to run the Beads CLI. It does not read exports, query Dolt directly, send issue data over the network, or maintain a second issue database. Live agent data comes from Herdr reactively. Project commands run on the space's machine; starting a remote session requires the launcher and a working Herdr window on that machine. Local worktree sessions are the verified configuration.

## Start and return to work

```sh
go-bd --herdr <issue-id>       # Start, resume saved conversation, or focus live session
go-bd <issue-id>               # Start/resume in the current terminal
```

Start resolves the full issue ID, checks readiness/inherited blockers, claims atomically, and creates or reuses its issue-named omp worktree. A self-owned, unblocked `in_progress` issue can resume after its previous agent ends. Other owners' claims are not overwritten.

Worktree directories, agent tabs, and worktree space names use the full bead ID, such as `myproject-123`, without an additional repository-name prefix.

A live associated Herdr omp session is focused instead of duplicated. Once that terminal ends, an unblocked, self-owned `in_progress` issue with saved session metadata resumes its exact conversation in the original worktree. The dashboard offers **Go to session**, **Resume**, or **Start**, in that order. In-progress issues without saved metadata still offer Start. Each launch pins `BEADS_DIR` to the main checkout's shared database. The launcher respects the repository's omp worktree configuration; configure `worktree.clone: false` when ignored Beads databases must not be copied. Existing foreign-repository/wrong-branch worktrees are refused.

### Epics are containers

Epics offer no Start or Resume action. Both direct and Herdr launch entrypoints reject epics before claiming or preparing a session/worktree. Start a concrete child task instead, including planning and investigation work. Existing legacy epic terminals remain focusable in the dashboard; their saved associations are preserved, not resumed or replaced.

`go-bd --reconcile-epics --json` returns read-only direct-child progress and per-epic errors from the canonical project database. Only `closed` children count as complete; actual parent edges determine membership, not dotted IDs. Nonempty all-closed epics report eligibility for **owner review**, but no refresh/service command closes, reopens, retries closure, or changes issue status. Empty epics are not eligible. Nested epic progress uses direct observed child states without inventing transitive closure.

Both legacy dashboard refresh and the independent service use `bin/beads_epics.py`'s `observe_epics(project)`. Explicitly registered sessionless projects and projects discovered from durable local runs are observed at startup, periodic passes, and validated-request wakeups. Per-project/per-epic errors remain visible without blocking other projects. This read-only policy supersedes earlier automatic epic-close/reopen behavior under the approved manual-completion scope.

Child counts and manual Beads closure are not one atomic transaction. Owners review current child state and normal Beads close gates separately; no force, raw SQL, or background repair bypass is used.

### Session association

The launcher labels the core terminal with `beads.project=<canonical .beads path>` and `beads.issue=<full issue ID>`. This explicit association survives app restarts. Sessions started before installation are not guessed from titles. To associate a known existing session manually:

```sh
hc label <terminal-address> beads.project=/absolute/project/.beads beads.issue=<full-issue-id>
```

### Persistent conversations

Both direct and Herdr launches load the explicit startup extension. It obtains the actual persistent session ID and transcript path from omp's lifecycle API, makes the transcript durable, and saves an object in the bead's `metadata.omp_session` with:

- `session_id`: the exact omp session ID, not a guessed newest session or ID prefix.
- `session_file`: the absolute saved transcript path.
- `machine`: a local identity stored in `${XDG_STATE_HOME:-~/.local/state}/go-bd/machine-id`.
- `worktree`: the original absolute worktree directory.

The metadata update merges only this key and preserves other metadata. A fresh task is delivered only after the transcript and metadata are saved; Herdr success also requires the startup acknowledgement. Primary-session switches/forks update the association; subagent sessions do not replace it.

Resume uses `omp --resume <session_file>` with no new startup task and no `--restore`: it reopens the conversation and waits for input rather than automatically restarting interrupted work. The original transcript ID, machine, directory, repository, and worktree branch are checked. Missing transcripts/worktrees, malformed records, foreign-machine records, or conflicting session ownership fail explicitly rather than starting fresh. Recorded history on a non-`in_progress` issue is not automatically replaced or resumed.

The ID is a pointer, not a backup. Keep the transcript and local machine identity; moving to another machine requires deliberate session transfer and association updates. Sessions started before this integration was installed are not retroactively discovered or guessed. The extension lifecycle/storage APIs were exercised with omp 18.17.0.


The dashboard counts actual live agents, not kept shells or finished terminals. Multiple associated sessions remain visible as a count, with the most urgent attention state first. Go to session and the status badge focus that agent.

Session states are independent of issue status:

| Badge | Meaning |
|---|---|
| Working | Agent is working |
| Needs you | Agent requests attention |
| Done · unseen | Agent finished a turn while you were away |
| Idle | Agent is not working; a previously unseen result may have been acknowledged |
| Starting | omp is running but has not reported agent status yet |

`Done · unseen` becomes `Idle` when acknowledged through Herdr. It never means the bead was closed. Session launching does not close tasks, merge code, remove worktrees, or push. Epic child-driven reconciliation is separate from session launching. Follow the repository's merge/close policy for executable work.

Per-project launch locks and terminal/dashboard receipts live under `${XDG_STATE_HOME:-~/.local/state}/go-bd`. Concurrent launches are refused rather than duplicated. A failed or unconfirmed launch reports an error and preserves any claimed issue, worktree, and terminal for inspection; retry focuses a live omp session if one exists. Slow launches can exceed Herdr exec's 30-second limit: inspect the associated terminal before retrying.

### Manual completion policy

The lifecycle controller below verifies integration and prepares an evidence-backed owner handoff: **Merged — awaiting manual closure**. Both offline local-target and GitHub PR verification remain required. Installed dashboard/launcher cutover and the concrete omp adapter remain tracked separately in `herdr-beads-2hv`, `herdr-beads-8vu`, and `herdr-beads-rlm`; the existing launcher does not verify merges or generate closure handoffs.

The owner reviews the current canonical database, full issue ID, ownership/run/submission, target, and merge evidence, then separately executes normal `bd close --reason …` without `--force`. Manual closure retains Beads' existing concurrency limitations: it is not atomically guarded against reopen, reassignment, or a newer run. A Herdr lock or a preceding status check does not remove that race. Observing an externally closed issue is not proof that a particular run owned its closure.

Automatic Bead closure, closure retries, workspace deletion, and destructive registry/terminal cleanup remain disabled, including after manual closure or explicit session release. Workspaces and transcripts stay intact for separate operator review and manual cleanup. The lifecycle delivery must keep retained resources inspectable after their Bead closes. Guarded automation is optional backlog (`herdr-beads-6yd`), not a release prerequisite; no upstream Beads patch or database topology change is required for the manual-completion scope.

## Durable local controller

The version-1 controller includes a private JSON run store, read-only integration observer, bounded Unix-socket request CLI, and installed launchd scheduler. Installation does **not** yet wire the legacy Start/Resume dashboard actions or extension to controller runs; their cutover remains separate. Unavailable concrete omp adapters expose unsupported capabilities without falling back to the legacy launcher.

```sh
# With the launchd service installed/running: one request in, one response out.
~/.local/share/herdr-beads-service/venv/bin/python \
  ~/.local/share/herdr-beads-service/bin/herdr-beads request < request.json
```

`request --socket /absolute/path/controller.sock` selects an isolated socket; default: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/controller.sock`. The socket parent must be owner-only (0700); the socket is 0600. The service accepts only local same-user peers on macOS/Linux and only versioned, validated run-scoped operations—not arbitrary commands or remote execution. Request/response limits are 1 MiB / 4 MiB, with a five-second client deadline. Exit codes: 0 for `ok`/`accepted`, 2 for `refused`, 1 for transport/dependency/IO errors. A timeout/disconnect is **not cancellation**: reuse the same request ID/payload or inspect status before acting. `bin/herdr-beads serve` is the low-level injectable transport, not the installed periodic scheduler; do not run competing owners on the installed socket.

The controller adopts the existing `go-bd/machine-id`; a missing/invalid identity refuses service startup rather than generating a replacement. Repository identity is the canonical checkout plus Git common directory, independently resolved canonical Beads directory, full issue ID, and machine. Linked worktrees resolve to the canonical source checkout; a clone-backed workspace needs the provider's independent registry/source validation.

### Versioned requests and independent state

The complete wire shapes live in `schemas/v1/{controller,run,adapter}.schema.json`. Every request has `apiVersion:1`, a unique `requestId`, canonical `scope`, `method`, and typed `params`. For example, status has no mutation precondition:

```json
{
  "apiVersion": 1,
  "requestId": "owner-status-1",
  "method": "status",
  "scope": {
    "repository": {"root": "/canonical/repo", "commonDir": "/canonical/repo/.git"},
    "beadsDir": "/canonical/repo/.beads",
    "issueId": "project-full-id",
    "machineId": "existing-go-bd-machine-id"
  },
  "params": {"runId": null}
}
```

Mutations carry `expected:{fence:{runId,attempt,generation},revision}`; only the first `start_or_focus` uses `expected:null`. Start additionally declares `harness:"omp"`, an explicit target, `newAttempt:false`, and `policy:{closeAfterIntegration:false,cleanupAfterRelease:false}`. Either policy set to true is refused. No request enables closure, push, fetch, merge, or resource deletion.

| Method | Effect |
|---|---|
| `status` | Current or retained run, latest durable operation, supported capabilities and action refusals; saved observations are timestamped, not a fresh authority check |
| `start_or_focus` | Reserve one eligible attempt, focus a validated live owner, or exact-resume an inactive writable session; no fallback conversation |
| `resume` | Exact saved conversation, new generation/binding; explicit read-only access requires current integration and adapter enforcement |
| `handoff` | Immutable source revisions, complete required set, exact workspace HEAD, summary/next steps/risks; neither release nor merge |
| `reconcile` | Fresh Beads/workspace/target/provider observations for the fenced run; invalidates unavailable/stale proof |
| `confirm_integration` | Trusted local owner receipt for rewritten local history; never agent prose or a callback assertion |
| `release` | Explicit original-owner relinquishment plus validated adapter acknowledgement, including revoked/failed reservations; retains resources |
| `observe` | Scoped credential/binding/sequence and adapter-validated neutral session/activity/release observations |

The socket always passes `trusted_human=False`: `confirm_integration` is refused with `untrusted_receipt` over this transport. An explicitly owner-authorized local embedding may call `Controller.handle(request, trusted_human=True)` after human review; JSON flags cannot enable that authority. A receipt alone does not prove integration: the observer independently verifies its exact resulting commit on the recorded target.

Execution/activity/access, required submission coverage, observed Beads closure, owner release, and cleanup are independent. Idle, turn-end, exit, lease expiry, branch disappearance and manual closure never imply release. A revoked incarnation cannot regain writable authority; its original owner can still explicitly release it. A new attempt requires prior acknowledged release and a distinct workspace plan. Historical attempts remain inspectable.

### Storage, serialization and recovery

`bin/beads_store.py` stores one versioned document per SHA-256 of canonical repository/database/full issue identity under `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/store`. Machine identity is validated inside the document, not used to hide foreign-machine conflicts. The current pointer, retained runs, request deduplication, operation intents/receipts, private bindings and audit history commit in one atomic replacement. State must remain outside repositories/workspaces. Files are 0600 and the store directory 0700; symlink/foreign/malformed/version-mismatched records are refused intact, never reset or quarantined.

Writes fsync the same-directory temporary file, atomically replace the snapshot, then fsync the directory. Permanent same-inode scope locks serialize requests; a separate permanent worker lock serializes external effects without holding the request lock across slow IO. Lock contention defers work rather than stopping the service. Same `(scope,requestId)` plus identical payload resolves to its original operation/result; a different payload refuses `request_id_conflict`. Completed results are historical receipts, not fresh authorization; consult current status/reconcile before owner action.

Each external step is journaled before execution and its observed receipt committed afterward. On restart an already-started step calls read-only `recover`, **never `perform` again**. An unknown outcome remains visibly pending with its operation ID; it cannot duplicate a worker or overwrite another attempt. A failed attempt retains claims/resources/history for review. The store keeps only a timestamped Beads status/assignee observation, not an authoritative issue copy. General transitions neither parse transcripts/process names nor read/write `metadata.omp_session`.

### Concrete adapter seam

`bin/beads_controller.py` exposes `Controller(machine_id, store=None, authority=None, execution=None, workspace=None, integration=None)`, `handle(request, trusted_human=False)`, `work_once()`, and `reconcile_runs()`. No registry or generic plugin system is involved. The installed launchd worker drains journaled effects and reobserves current runs under the existing worker lock; observation commits require the same run fence and revision, so a concurrent handoff/callback cannot resurrect old proof. Semantic changes are durable; identical polls update health only. Reconciliation retains generic Git path/common-dir/branch validation without requiring omp's provisioning/registry capability, which is still required for execution and never grants cleanup authority.

- Execution declares `capabilities`: `launch`, `exact_resume`, `durable_identity`, `owner_activity`, `structured_handoff`, `explicit_release`, `read_only`. Missing capability/harness returns a structured refusal before unsafe work.
- Workspace `plan(run)` is read-only and returns the exact schema `Workspace` to journal before allocation; `validate(run)` checks the recorded provider/backing/source/registry identity. Git path/common-dir/branch checks also run in the controller.
- Execution/workspace implement `perform(step, run, intent, private)` and read-only `recover(...)`. Steps are `workspace`, `launch`, `resume`, `focus`, `release`, `read_only`; Beads claim is the controller authority's own step. `intent` supplies the operation ID, fence and journal position; private state supplies the planned workspace, scoped binding/credential, sequence, and adapter-owned association.
- A receipt is `{"workspace": Workspace}` or `{"observation": Observation}` as appropriate; focus completes only after the adapter verifies/focuses the exact live owner. Unknown outcomes return null. Optional `receipt.private` merges into the adapter-owned private association, including when a matching callback raced its receipt.
- `validate_observation(run, observation, private)` independently verifies actual durable session/terminal/workspace/ownership and acknowledgements. JSON shape or callback credential alone is insufficient proof. The controller validates fence, binding, event ID and monotonic sequence; secrets/transcript paths never appear in public run/operation responses.
- Omp transcript/lease/process recognition, initial task delivery after durable acknowledgement, exact resume arguments, and field-preserving `metadata.omp_session` mirroring remain exclusively adapter/adoption responsibilities. This controller never rewrites Beads metadata, so unrelated keys remain untouched.

### Integration evidence and manual owner handoff

Targets are explicit: `local-target` binds canonical repository/full `refs/heads/...`; `github-pr` additionally binds host/stable repository ID/owner/name, PR number/URL, expected base and exact submitted head; explicit `remote-target` retains remote authority. Remote presence does not select a mode. Local mode requires no remote, provider fields, credentials or network.

Handoff requires unique immutable submission revisions, all for the explicit target. Required sources must exist in workspace history and the required set must include the exact workspace HEAD (supports stacked/multiple submissions without silently omitting newer work). A changed mode/target/head/required set invalidates aggregate proof and preserves prior evidence in audit. Every required member needs fresh current-fence evidence; one merged PR does not complete another submission.

`bin/beads_integration.py` performs read-only verification: local source ancestry in a pinned canonical target commit; explicitly trusted rewritten-history mappings independently checked on that target; genuine `gh api` merged/time/repository/base/head/merge-commit evidence for PRs; or explicit `git ls-remote` published-target observation plus already-local objects. It never fetches missing objects or downgrades authority when offline. Provider/auth/network failure holds proof visibly. Local integration is not publication/backup or Beads synchronization.

Verified complete coverage produces read-only access, an adapter-enforcement request when supported, and `closure.state:"awaiting_manual"` with **Merged — awaiting manual closure**. The run contains canonical database/full issue ID, owner/fence, explicit target, exact required submissions and their evidence. `closure.manualCommand` is a safely quoted normal `bd close --reason` command **for owner review only**. Service health exposes these identities/evidence alongside timestamped per-run/project errors; the run retains its evidence timestamp when an identical later observation is revalidated. Status is not a revision-guarded close capability.

External manual closure is only `closure.state:"observed_closed"`; the controller does not assert closure ownership or issue/retry a close. Reopen, reassignment, changed submissions and target resets revoke/invalidate old proof rather than reclose. Cleanup stays `held` with `manual_cleanup_required` after integration, closure and release. No workspace, branch, registry, session or terminal cleanup method exists here.

Observed validation: deterministic recovery/concurrency/refusal tests, actual socket/CLI accepted-operation smoke with a deliberately delayed deterministic execution boundary, real offline local fast-forward proof, and genuine read-only GitHub PR evidence ([cli/cli#8294](https://github.com/cli/cli/pull/8294)) surviving store reopen. The latter is observer verification, **not** managed-project PR/cutover acceptance. Real omp/session resume and combined local/PR/UI acceptance belong to the concrete adapter and cutover tasks; they are not inferred from these storage/transport checks.

## Verification

```sh
cd ~/.local/share/herdr-beads
herdr-app check ~/.config/herdr-app/mods/beads-dashboard
herdr-app check ~/.config/herdr-app/mods/bead-detail
herdr-app check ~/.config/herdr-app/mods/beads-sidebar
node --test model.test.mjs
python3 launcher_test.py
python3 -m unittest epic_test
python3 -m unittest controller_test store_test transport_test service_test service_install_test
```

The native check only covers the initial render. Also open the dashboard in Herdr, switch between All open and Ready, check Ready against the union of `bd ready --limit 0` and `in_progress` issues, and select two issues to confirm detail-pane reuse and formatted Markdown. Check that blocked, in-progress, and deferred issues remain available in All open and closed issues do not appear as matches. For changes to session launching, also exercise a real worktree agent. Session attention states must be observed in the running app, not inferred from an issue's `in_progress` field.

Use actual pointer clicks for this check, including the first click after focusing another pane. Dashboard background focus is handled on click, not mouse-down, to avoid focus-driven redraws during a pending control click. Issue rows stop propagation so selecting a bead does not refocus the dashboard after opening its details. Mod commands alone do not verify pointer dispatch.

For list-rendering changes, verify a loaded viewport in the running app, not just an initial-render check: rows must remain visible after refresh and resizing, disclosure arrows must fold/unfold children, and selection/session buttons must still work. The virtual-list callback must only mount retained row views; row handlers belong to those views, and row actions/reconciliation run in the dashboard task. The list height is measured from its viewport.

The action queue drains in a dashboard-owned native timer callback every 50 ms; idle ticks do not notify or redraw. Do not replace it with a JavaScript promise wake, even with `cx.sleep()`: a promise resolved by a row callback can retain that row's native ownership scope. Filtering/folding that row can then release sibling entities and kill the action loop on its next update. Do not start timers during render either. Verify repeated row selections interleaved with filtering/folding and refreshes; controls must keep responding and rows must reappear. Node-only model tests do not exercise this native ownership boundary.

For navigation, follow parent/dependent/child IDs, including a closed or filtered-out issue under folded ancestors. Verify that the target is highlighted and scrolled into view without inflating filter counts. Use Back/Forward across both list and relationship selections, then go Back and choose a new bead to confirm that Forward becomes unavailable. Re-selecting the current bead must not add a history step.

For persistence changes, use an isolated project to start a real session, remember a unique token, terminate the terminal, and resume. Verify the same session ID and worktree, token recall without resending it, preservation of unrelated metadata, live-session focus without duplication, and visible errors for missing transcripts or a different machine. Exercise both direct and Herdr launches and observe the dashboard's Start → Go to session → Resume transitions.

