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
- Stable machine identity: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/machine-id`, imported unchanged from `go-bd/machine-id` when present. Both files are retained; conflicting/invalid identities refuse startup.

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

The mod uses Herdr's `exec` API for Beads reads; it does not read exports or query Dolt directly. Attention comes from Herdr reactively and does not grant lifecycle authority. Browsing remains machine-scoped; controller execution is local-machine only, with an explicit remote-machine refusal and no local-launch fallback.

### Controller dashboard source — installation held for cutover

`mods/beads-dashboard` now uses `bin/beads_ui.py` and controller-v1 for actions and lifecycle state. This change does **not** replace installed mods, the launcher, or the running production service; coordinated installation remains `herdr-beads-rlm.4`. The default bridge/interpreter paths are `~/.local/share/herdr-beads-service/bin/beads_ui.py` and its `venv/bin/python`. Development tabs can explicitly pass `lifecycleBridge`, `lifecyclePython`, and `lifecycleConfig` props; `dashboardMod` identifies a separately named development copy.

- **Actions** inspects an issue through controller status. Start, focus, exact resume, structured submission, integration verification, and explicit owner release follow controller-advertised availability/refusals. Only **omp** is supported; another agent appearing in Herdr is not a supported execution harness. Missing adapter capabilities remain unavailable, not a harness picker or launcher fallback.
- The destination is shown before starting. The default is explicit `local-target` on the canonical repository's `refs/heads/master`; **Configure explicit destination** edits a Target JSON object for new runs. `git config herdr.beads.target '<Target JSON>'` supplies a project default. A remote never selects PR mode. Local mode requires no PR, `gh`, authentication, or network; PR mode records provider host/stable repository ID/owner/name and full expected base ref.
- **Submit local handoff** / **Submit PR handoff** opens a reviewable structured form with the exact workspace HEAD and complete previously required submission set. PR submissions require the genuine PR number/URL. A handoff can explicitly change mode/target/submissions; it does not merge, push, or attest integration. The controller rejects stale fence/revision/coverage or mismatched source history.
- **Lifecycle** is separate from All open/Ready counts and tree/navigation context. Its compact retained-run list keeps closed Beads inspectable, with exact `manual_cleanup_required` and other blockers. Details expose canonical database/full issue ID, owner/run/generation, selected target, required submissions, mode-specific evidence, release and observed issue state. A finished turn is never merge proof.
- Controller snapshots, operation errors, request IDs and operation IDs survive tab recreation, scoped to machine/database/canonical repository. Browsing and lifecycle failures are separate; failed refreshes keep last-good data visibly read-only. An uncertain request can be recovered with **Recover same request**, preserving its ID and payload; it is never silently replaced with another start. Service loss never launches locally.
- Complete verified coverage shows **Merged — awaiting manual closure**. The closure button **copies only** the controller's safely quoted normal `bd close --reason` command. Its limitation is explicit: normal manual Beads closure is not revision/run guarded. Herdr never executes/retries closure or workspace/registry/terminal deletion. External closure is an observation, not ownership proof; retained resources still require separate operator-reviewed manual cleanup. Reopen, reassignment or invalidated evidence suppress stale closure handoffs.
- Rewritten local history remains **Integration needs confirmation**, distinct from **Awaiting PR merge** and provider errors. The details explain the trusted local owner-receipt path; the socket/UI cannot attest it. See the controller receipt contract below.

Observed source-dashboard smoke (2026-11-09): a separately named native development mod and isolated real controller exercised start, focus without duplication, exact-session resume, structured local and PR submissions, copy-only closure, tab recreation, filter counts and detail-pane reuse. Offline local proof used a no-remote fixture target at `61e687ee8e6f64eaba3ed2072e772d87d97386eb`, first visibly held for confirmation and then advanced by the fixture driver; genuine read-only PR proof used existing [NVIDIA/OpenShell#5077](https://github.com/NVIDIA/OpenShell/pull/5077), exact head `958efaa3d826cdc067fe9f357e5db41ce23141ee`, repository ID `1166129534`, and `refs/heads/windows`. External fixture-only normal Beads closure reduced All open/Ready counts without hiding its retained run; reopening invalidated the handoff without reclosure or deletion. Real startup exposed a recovery race that rewound an accepted binding to `starting`; the controller now preserves that binding/turn state, with a failing-before/passing-after regression. Native commands and screenshots exercised the running surface; pointer dispatch and production/managed-project cutover acceptance are not inferred from this isolated smoke.

Stopping the isolated service retained inspectable last-good runs and disabled lifecycle actions/manual-command copying. A remote-machine tab visibly refused without a local run. Removing only the fixture's configured `gh` capability held the PR run with its exact provider error while the local run remained independently retained; restoring it reverified the genuine PR evidence. Operation errors survived ordinary Beads refreshes. Final checks: 175 Python tests, 57 JavaScript tests, and all three native mod checks passed. Explicit release/read-only enforcement remain unavailable where the concrete adapter reports those capability gaps; no fallback or destructive cleanup was attempted.

## Start and return to work

```sh
go-bd --herdr <issue-id>       # Start, resume saved conversation, or focus live session
go-bd <issue-id>               # Start/resume in the current terminal
```

Start resolves the full issue ID, checks readiness/inherited blockers, claims atomically, and creates or reuses its issue-named omp worktree. A self-owned, unblocked `in_progress` issue can resume after its previous agent ends. Other owners' claims are not overwritten.

Worktree directories, agent tabs, and worktree space names use the full bead ID, such as `myproject-123`, without an additional repository-name prefix.

The installed legacy launcher focuses a live associated omp session or resumes its exact saved conversation in the original worktree. The controller dashboard source instead uses controller actions, durable run/session identity and capability refusals; it never reads `metadata.omp_session` or checks process/transcript names to choose an action. Each launch pins `BEADS_DIR` to the canonical shared database. The concrete workspace adapter respects omp configuration and refuses foreign-repository/wrong-branch resources.

### Epics are containers

Epics offer no Start or Resume action. Both direct and Herdr launch entrypoints reject epics before claiming or preparing a session/worktree. Start a concrete child task instead, including planning and investigation work. Existing legacy epic terminals remain focusable in the dashboard; their saved associations are preserved, not resumed or replaced.

`go-bd --reconcile-epics --json` returns read-only direct-child progress and per-epic errors from the canonical project database. Only `closed` children count as complete; actual parent edges determine membership, not dotted IDs. Nonempty all-closed epics report eligibility for **owner review**, but no refresh/service command closes, reopens, retries closure, or changes issue status. Empty epics are not eligible. Nested epic progress uses direct observed child states without inventing transitive closure.

The installed legacy dashboard's progress command and the independent service use `bin/beads_epics.py`'s `observe_epics(project)`; the controller dashboard consumes the service's scoped progress snapshot. Explicitly registered sessionless projects and projects discovered from durable local runs are observed at startup, periodic passes, and validated-request wakeups. Per-project/per-epic errors remain visible without blocking issue browsing. This read-only policy supersedes earlier automatic epic-close/reopen behavior under the approved manual-completion scope.

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

The extension publishes the durable transcript binding to the controller; its execution adapter owns the field-preserving `metadata.omp_session` mirror. A fresh task and launcher acknowledgement require the controller's exact-session acknowledgement after validation and metadata publication. The extension makes no Beads mutations. Subagents cannot replace the primary association. Switch, fork/branch, and tree navigation are explicitly refused while mapped: they require a new controller binding, not an implicit conversation replacement.

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

The version-1 controller includes a private JSON run store, concrete omp execution/workspace adapters, read-only integration observer, bounded Unix-socket request CLI, controller-connected omp startup extension, and installed launchd scheduler. Foreground `serve` and the installed service compose the adapters; installation does **not** yet wire legacy Start/Resume dashboard actions to controller runs or migrate legacy runs. Those cutovers remain separate. Only omp is supported; missing tools or capabilities refuse rather than substituting a harness or falling back to the legacy launcher.

```sh
# With the launchd service installed/running: one request in, one response out.
~/.local/share/herdr-beads-service/venv/bin/python \
  ~/.local/share/herdr-beads-service/bin/herdr-beads request < request.json
```

`request --socket /absolute/path/controller.sock` selects an isolated socket; default: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/controller.sock`. The socket parent must be owner-only (0700); the socket is 0600. The service accepts only local same-user peers on macOS/Linux and only versioned, validated run-scoped operations—not arbitrary commands or remote execution. Request/response limits are 1 MiB / 4 MiB, with a five-second client deadline. Exit codes: 0 for `ok`/`accepted`, 2 for `refused`, 1 for transport/dependency/IO errors. A timeout/disconnect is **not cancellation**: reuse the same request ID/payload or inspect status before acting. `bin/herdr-beads serve` is the low-level injectable transport, not the installed periodic scheduler; do not run competing owners on the installed socket.

Each socket message is one newline-terminated JSON object. Clients send the request and read the response without a write-side shutdown: the server may respond and close immediately, and on Darwin a subsequent `SHUT_WR` can fail with `ENOTCONN` even while the complete response remains buffered. Requests are not automatically retried.

The controller retains the existing machine UUID independently of the retired launcher's name in `herdr-beads/machine-id`. Import uses fsynced, exclusive atomic publication; it never replaces an existing identity. Both canonical and legacy files are checked while present; a missing/invalid established identity or disagreement refuses startup. Only a genuinely fresh service install may create a UUID. Repository identity is the canonical checkout plus Git common directory, independently resolved canonical Beads directory, full issue ID, and machine. Linked worktrees resolve to the canonical source checkout; a clone-backed workspace needs the provider's independent registry/source validation.

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

Mutations carry `expected:{fence:{runId,attempt,generation},revision}`; the first `start_or_focus` or `adopt_legacy` uses `expected:null`. A new start declares `harness:"omp"`, an explicit target, `newAttempt:false`, and `policy:{closeAfterIntegration:false,cleanupAfterRelease:false}`. Either policy set to true is refused. No request enables closure, push, fetch, merge, or resource deletion.

| Method | Effect |
|---|---|
| `status` | Current or retained run, latest durable operation, supported capabilities and action refusals; saved observations are timestamped, not a fresh authority check |
| `adopt_legacy` | Journal adoption of already self-owned in-progress omp history; validate exact session and independent workspace, focus live owner without launching; no claim or destination inference |
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
- Legacy adoption uses read-only `execution.adopt(run, intent, private)` for a neutral session/terminal/activity plus private evidence, then independent `workspace.adopt(run, recordedPath)` for actual provider/backing/source/branch identity. The journal's `adopt` step is replayable without launching; only a validated live result schedules focus.
- Execution/workspace implement `perform(step, run, intent, private)` and read-only `recover(...)`. Steps are `workspace`, `launch`, `resume`, `focus`, `release`, `read_only`; Beads claim is the controller authority's own step. `intent` supplies the operation ID, fence and journal position; private state supplies the planned workspace, scoped binding/credential, sequence, and adapter-owned association.
- A receipt is `{"workspace": Workspace}` or `{"observation": Observation}` as appropriate; focus completes only after the adapter verifies/focuses the exact live owner. Unknown outcomes return null. Optional `receipt.private` merges into the adapter-owned private association, including when a matching callback raced its receipt.
- `validate_observation(run, observation, private)` independently verifies actual durable session/terminal/workspace/ownership and acknowledgements without effects. Optional `publish_observation(...)` runs only after policy accepts the validated event, before acknowledging the request; the omp adapter uses it for field-preserving canonical session mirroring. JSON shape or callback credential alone is insufficient proof. The controller validates fence, binding, event ID and monotonic sequence; secrets/transcript paths never appear in public run/operation responses.
- Omp transcript/lease/process recognition, initial task delivery after durable acknowledgement, exact resume arguments, and field-preserving `metadata.omp_session` mirroring remain exclusively adapter/adoption responsibilities. General controller policy never interprets or rewrites omp metadata.

`bin/beads_omp.py:OmpExecution`, `bin/beads_workspace.py:OmpWorkspace`, and `bin/beads_herdr.py:HerdrHosting` are separate concrete dependencies, not a plugin framework. `create_adapters(executables)` supplies static service composition with explicit absolute executable paths; null tools never fall back to ambient PATH. Launch, exact resume, durable identity, owner activity and structured handoff are supported. Read-only enforcement and explicit release remain visibly unsupported by this adapter until the extension-control integration is installed.

Before terminal creation, the adapter fsyncs an owner-only per-run/generation startup config and initial task outside the workspace under `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/omp`. An interrupted or slow Herdr request reconnects by the exact `GO_BD_SESSION_CONFIG` value in the terminal's immutable creation argv, including the creation-to-labeling gap. Recovery never issues another terminal creation request. Unconfirmed operations retain their claims, workspace, transcript and terminal for inspection.

Start/focus/resume recheck the canonical database, owner, blockers and workspace identity. Focus requires the acknowledged foreground omp PID and exact transcript lease, not a title or stale agent report. Ended terminals resume only the recorded absolute transcript with `--resume`, no fresh task or `--restore`; missing, foreign, conflicting or leased history refuses intact. Existing legacy associations require explicit adoption rather than a new-run overwrite.

### Legacy adoption

Send `method:"adopt_legacy", params:{"expected":null}` with the same canonical scope as `status`. Adoption records intent before inspecting legacy state; restart repeats read-only reconciliation rather than launching. Completion retains the concrete omp evidence privately and records the neutral session/workspace result before any later cutover. No legacy JSON, transcript, lease, Beads metadata, branch, registry or machine-ID file is retired or rewritten. The legacy issue lock serializes observation with an in-flight old launch.

Sources are the exact `metadata.omp_session`, per-database `go-bd/<sha256(beadsDir)>/issue-<full-id>.json`, original launch config/ack, terminal creation argv, canonical `beads.project`/`beads.issue` labels, foreground omp PID, exact transcript lease, and independently validated omp workspace registry/Git backing. A live terminal requires agreement among these identities, not merely generic Herdr labels, a title or a recent transcript. Conflicting, malformed, foreign-machine, missing or orphaned resources refuse visibly and remain retained for explicit resolution. Malformed launch files in the project's legacy state conservatively block adoption even when their issue cannot be determined.

An adopted live run focuses its existing terminal; repeating adoption focuses the same incarnation. An ended, unleased exact conversation is adopted stopped, without creating a terminal. A subsequent `resume` or `start_or_focus` reopens only the original transcript/workspace, without a fresh prompt. Repeated `adopt_legacy` on a retained inactive run uses that same exact-resume path. Original branch names and actual linked/clone backing are preserved; new workspace planning keeps its stricter issue/attempt naming checks.

Adopted runs start with `target:null`, no submissions/evidence, held release and `manual_cleanup_required`. Use `target:null` and the retained policy when focusing/resuming an unbound run with `start_or_focus`; null is refused for new attempts. Explicit `handoff` supplies mode/target and exact required submissions. Neither a remote nor a branch name chooses local-target versus PR mode. Adoption cannot produce merge proof, a manual closure command or cleanup permission.

The dashboard preserves an adopted run's null destination even when a project default is configured. Its lifecycle panel shows the unbound target without assuming a mode; the handoff editor leaves destination fields unbound for explicit owner input before controller validation.

Observed adoption validation (2026-11-09): two actual CLI/socket requests adopted/focused the running omp conversation `01a1c269-5da9-710c-a5b4-7f86278ef4b9` in terminal `t2n` using an isolated controller store. Transcript bytes, legacy JSON and the terminal set were unchanged; canonical linked backing and original branch were validated, and destination remained null. This is live adoption proof, not installed cutover or managed-PR integration acceptance.


Workspace planning preserves the explicit integration target and branches new work from its already-local ref; no remote, fetch, push, PR creation or merge is required for local-target mode. Registry/source/common-dir/branch checks distinguish independent clones from linked workspaces; clone origin never selects the integration destination. Omp 18.17's copy-on-write “clone” provisioning still registers linked Git worktrees. Supported config queries can initialize omp settings storage, and `omp worktree add` runs the user's existing checkout hooks; the adapter itself issues no network or integration commands.


### Omp session extension

`bin/go-bd-session.mjs` is owned, implemented, and packaged by **herdr-beads**, not omp-core. `install.sh` copies it beside the legacy launcher at `~/.local/bin/go-bd-session.mjs` and packages the service copy beside its execution adapter at `${HERDR_BEADS_INSTALL_ROOT:-~/.local/share/herdr-beads-service}/bin/go-bd-session.mjs`. The concrete omp adapter loads that adjacent copy explicitly with `omp --extension /absolute/path/go-bd-session.mjs`. Its supported API baseline is [omp 18.17.0](https://github.com/can1357/oh-my-pi/blob/v18.17.0/docs/extensions.md): `ensureOnDisk`, `FileSessionStorage` leases, main/sub identity, lifecycle hooks, managed timers, async-job snapshots, and registered commands. There is no ambient discovery, second harness, or universal harness claim.

This extension requires controller-v1 launch configuration. Do not deploy it with the old direct-mutation launcher: missing configuration or acknowledgement visibly terminates the session before startup work. Concrete adapters and service packaging are present; coordinated legacy dashboard/launcher cutover remains separate (`herdr-beads-rlm.4`). No legacy protocol fallback is provided.

`GO_BD_SESSION_CONFIG` names an absolute, owner-only JSON file in an owner-only directory outside the workspace. The adapter supplies:

- `schemaVersion:1`, canonical `scope`, exact `fence`, private `bindingId`/`credential`, and absolute `controllerSocket`.
- Canonical `worktree`, `previous` (exact prior `omp_session` or null), absolute startup `ack`, optional `prompt_file`, and `access` (`writable` or `read_only`).
- Neutral `terminal:{machineId,address,spaceId,tabId}`. If null, only actual `HC_TERMINAL_ID` supplies the local address; absent identity refuses startup.

The extension claims the writer's session lease before `ensureOnDisk`, then validates the actual transcript header/ID/canonical directory. It atomically writes and fsyncs `<config>.binding.json` before sending a fenced, credentialed, monotonically sequenced `observe`. This private record carries the neutral `binding`, omp transcript identity, PID, latest `observation`, sequence, access/frozen/released gates, and `acknowledged` flag. The controller execution adapter independently validates it and publishes/verifies the field-preserving metadata mirror before acknowledging; a callback secret alone is not ownership proof. Only then does the extension mark acknowledged and publish the immutable `session_bound` observation in `ack.observation`. It never invokes `bd` or independently competes to update the association.
Each serialized action shares one 25-second controller acknowledgement budget across its requests, below omp's 30-second lifecycle-handler limit. Slow publication may exceed the transport CLI's five-second client deadline; this extension connects directly and waits within its action budget. A missing, refused, or late acknowledgement still fences/exits visibly without dispatching the startup task. No retry creates another request, event, or session identity.

The main session receives Bead/database/run context before each prompt. Commands:

| Command | Contract |
|---|---|
| `/bead-context` | Current same-fence controller state and ownership policy; no mutation |
| `/bead-handoff <JSON>` | Full `HandoffParams` from `schemas/v1/controller.schema.json`, **without** `expected`; the extension obtains the current same-fence revision |
| `/bead-release <reason>` | Explicit owner relinquishment request, refused while streaming, queued input, running async work, pending delivery, or unknown job ownership exists |

For example, a local handoff has this shape (replace identities and SHAs with real values; the controller checks actual workspace HEAD/history):

```json
{
  "previousCoverageRevision": 0,
  "target": {
    "mode": "local-target",
    "repository": {"root": "/canonical/repo", "commonDir": "/canonical/repo/.git"},
    "ref": "refs/heads/main"
  },
  "workspaceHead": "0123456789abcdef0123456789abcdef01234567",
  "requiredSubmissions": [{
    "submissionId": "review-1",
    "submissionRevision": 1,
    "head": "0123456789abcdef0123456789abcdef01234567",
    "target": {
      "mode": "local-target",
      "repository": {"root": "/canonical/repo", "commonDir": "/canonical/repo/.git"},
      "ref": "refs/heads/main"
    },
    "pullRequest": null
  }],
  "summary": "Ready for owner review",
  "nextSteps": ["Owner integrates into the recorded target"],
  "risks": []
}
```

Local-target needs no remote, GitHub metadata, network, or credentials. For `github-pr`, both targets instead bind `repository:{host,repositoryId,owner,name}` and the full expected base `ref`; each submission must include exact `pullRequest:{number,url}` and submitted `head`. Mode/target/coverage changes are explicit, never inferred from a Git remote. No command merges code or attests integration; rewritten local history requires a separate trusted operator receipt, independently verified on the target.

The controller adapter writes `<config>.control.json` with `{fence,bindingId,requestId,command:"read_only"|"release"}`. Managed polling reads only this private control channel, never merge status. Read-only enforcement freezes new tool calls, aborts the turn, and acknowledges only once owned work is inactive. Release requires a preceding controller owner request, rechecks idle state after IO, and durably freezes the primary/subagent tool gate before emitting `release_acknowledged`. A failed/ambiguous request stays fenced for inspection; it never silently reopens writable authority. Resources and the lease remain retained until process shutdown.

Idle/`agent_end` only reports turn attention while ownership remains active. `session_stop`, shutdown, and timers are not merge or release evidence. Shutdown skips a busy owner queue and otherwise spends at most 500 ms awaiting its exit notification; it releases the local lease without waiting for merge, issuing release, closing Beads, or deleting anything. The durable binding remains available for independent process/lease reconciliation after a missed notification.

Observed extension validation: actual omp 18.17.0 TUI context/handoff/release commands against an isolated local protocol peer, actual persistent transcript/lease handling, and delayed startup with a six-second acknowledgement before the launcher receipt. Earlier real metadata-mirror validation preceded its transfer to the controller execution adapter. This is adapter-surface evidence, not production-controller/Herdr cutover or genuine managed-PR merge acceptance.


### Integration evidence and manual owner handoff

Targets are explicit: `local-target` binds canonical repository/full `refs/heads/...`; `github-pr` additionally binds host/stable repository ID/owner/name, PR number/URL, expected base and exact submitted head; explicit `remote-target` retains remote authority. Remote presence does not select a mode. Local mode requires no remote, provider fields, credentials or network.

Handoff requires unique immutable submission revisions, all for the explicit target. Required sources must exist in workspace history and the required set must include the exact workspace HEAD (supports stacked/multiple submissions without silently omitting newer work). A changed mode/target/head/required set invalidates aggregate proof and preserves prior evidence in audit. Every required member needs fresh current-fence evidence; one merged PR does not complete another submission.

`bin/beads_integration.py` performs read-only verification: local source ancestry in a pinned canonical target commit; explicitly trusted rewritten-history mappings independently checked on that target; genuine `gh api` merged/time/repository/base/head/merge-commit evidence for PRs; or explicit `git ls-remote` published-target observation plus already-local objects. It never fetches missing objects or downgrades authority when offline. Provider/auth/network failure holds proof visibly. Local integration is not publication/backup or Beads synchronization.

Local proof reads the exact canonical Git common directory and a direct full branch ref, pins its commit, checks source (or receipt-result) ancestry, then rereads the authority before returning evidence. Clone-local branches, symbolic target redirects, replacement refs and grafts cannot supply proof. Git reads disable implicit promisor-object fetches. Missing objects hold verification. Remote-target proof rereads the explicitly recorded published ref; tracking refs, remote aliases and URL rewrites are not authority.

GitHub uses an explicit uncached GET for the recorded host/repository/PR, verifies stable repository identity and web/API URLs, and requires `state=closed`, boolean `merged=true`, a valid nonfuture merge time and exact submitted head. The provider result commit proves merge, squash and rebase equally; source ancestry is not required. Deleted fork repositories are supported. HTTP responses with missing/old/future Date (60-second clock tolerance), nonzero Age, cache-hit/stale indicators or ambiguous records hold proof rather than reuse it. Local clock skew can therefore require correction before PR verification succeeds. PR bodies never select a Bead or authorize completion.

After provider observation, reconciliation resamples workspace HEAD/cleanliness and Beads ownership/status before preparing the handoff. Fresh revalidation of identical proof retains its original evidence identity/timestamp and stable handoff/command; it never substitutes cached proof for a provider/target read. Reset targets, failed lookups, new unsubmitted commits and changed ownership clear the current handoff and command, with an actionable review reason. Evidence is a timestamped observation, not an atomic lock on Git/GitHub/Beads.

Verified complete coverage produces read-only access, an adapter-enforcement request when supported, and `closure.state:"awaiting_manual"` with **Merged — awaiting manual closure**. `closure.handoff` snapshots canonical database/full issue ID, expected owner/run/attempt/generation, coverage revision/workspace HEAD, explicit mode/target and all exact required submissions with their validated evidence. Local proof includes the pinned integrated target commit and any trusted rewritten-history receipt; PR proof includes provider repository/PR URL, expected base/head and actual merge result. A content-derived `handoffId` and original `createdAt` remain stable across unchanged reconciliation and restart. Historical snapshots stay in the durable audit when invalidated or externally closed; they are not current proof. Existing v1 records without the optional snapshot can be read and receive one on fresh verified reconciliation; no Beads database migration is involved.

`closure.manualCommand` is a safely shell-quoted normal `BEADS_DIR=… bd --sandbox -C … close --reason … -- <full-issue-id>` command **for owner review only**. Its reason records exact scope/owner/fence/submission/target evidence. `/bead-context` in the concrete omp extension displays the readable handoff, standalone copyable command, concurrency warning and review instructions, with observed Beads status, merge coverage and retained/manual-cleanup state shown separately. Held or externally closed states do not display a stale command. Service health exposes the same run identities/evidence alongside timestamped per-run/project errors.

Before executing the command separately, the operator reviews current database/issue, actor/assignee, status, run/submissions and authoritative evidence. Normal manual closure is **not atomically revision/run guarded**: reopen, reassignment or a newer run/submission can race that review. Normal Beads owner/dependency gates still apply; there is no force, SQL, batch close, status update to closed, hidden retry or UI/controller/service/extension closure executor.

External manual closure is only `closure.state:"observed_closed"`; the controller does not assert closure ownership or issue/retry a close. Reopen, reassignment, changed submissions and target resets revoke/invalidate old proof rather than reclose. Cleanup stays `held` with `manual_cleanup_required` after integration, closure and release. No workspace, branch, registry, session or terminal cleanup method exists here.

Observed validation includes deterministic recovery/concurrency/refusal tests, actual socket/CLI delayed-operation recovery, offline local fast-forward proof, and genuine read-only GitHub PR evidence ([cli/cli#8294](https://github.com/cli/cli/pull/8294)) surviving store reopen. The latter remains observer verification, **not** managed-project PR/cutover acceptance.

Concrete adapter smoke (2026-11-09, omp 18.17): actual Herdr start → focus → killed-terminal exact resume retained workspaces, transcript IDs, token recall and unrelated metadata for both a no-remote `local-target` (`refs/heads/integration-local`) and an explicitly configured `github-pr` target (`cli/cli`, stable repository ID `212613049`, `refs/heads/trunk`). The socket service was reopened without losing either run. A focus exceeding the client deadline reconnected to the same succeeded operation. This proves execution/target retention, not creation/merge of a managed PR or installed dashboard/extension cutover.

Additional observer acceptance exercised real offline normal/fast-forward merges and exact local squash receipts (including reset invalidation), plus genuine provider records for [merge](https://github.com/kubernetes/kubernetes/pull/143983), [squash](https://github.com/python/cpython/pull/161674) and [rebase](https://github.com/zulip/zulip/pull/40635). These are independent adapter observations, not a claim that this project's run was merged or that managed-project/UI cutover passed.

Manual-handoff smoke additionally exercised a real disposable Beads database: offline verified integration → stable command after restart/reconciliation → separately executed normal owner close → observed external closure → reopen without reclosure, retaining the workspace. A genuine read-only [cli/cli#8294](https://github.com/cli/cli/pull/8294) submission imported into a disposable checkout produced the PR handoff and survived restart. Actual omp 18.17 TUI `/bead-context` rendered its verified status, concurrency warning and copyable command without a model turn or closure action. These are disposable policy/presentation acceptance tracks, not creation/merge of a new managed-project PR or installed dashboard cutover.


## Verification

```sh
cd ~/.local/share/herdr-beads
herdr-app check ~/.config/herdr-app/mods/beads-dashboard
herdr-app check ~/.config/herdr-app/mods/bead-detail
herdr-app check ~/.config/herdr-app/mods/beads-sidebar
node --test model.test.mjs lifecycle.test.mjs session.test.mjs
python3 launcher_test.py
python3 -m unittest epic_test
python3 -m unittest discover -p '*_test.py'
```

The native check only covers the initial render. Also open the dashboard in Herdr, switch between All open and Ready, check Ready against the union of `bd ready --limit 0` and `in_progress` issues, and select two issues to confirm detail-pane reuse and formatted Markdown. Check that blocked, in-progress, and deferred issues remain available in All open and closed issues do not appear as matches. For changes to session launching, also exercise a real worktree agent. Session attention states must be observed in the running app, not inferred from an issue's `in_progress` field.

Use actual pointer clicks for this check, including the first click after focusing another pane. Dashboard background focus is handled on click, not mouse-down, to avoid focus-driven redraws during a pending control click. Issue rows stop propagation so selecting a bead does not refocus the dashboard after opening its details. Mod commands alone do not verify pointer dispatch.

For list-rendering changes, verify a loaded viewport in the running app, not just an initial-render check: rows must remain visible after refresh and resizing, disclosure arrows must fold/unfold children, and selection/session buttons must still work. The virtual-list callback must only mount retained row views; row handlers belong to those views, and row actions/reconciliation run in the dashboard task. The list height is measured from its viewport.

The action queue drains in a dashboard-owned native timer callback every 50 ms; idle ticks do not notify or redraw. Do not replace it with a JavaScript promise wake, even with `cx.sleep()`: a promise resolved by a row callback can retain that row's native ownership scope. Filtering/folding that row can then release sibling entities and kill the action loop on its next update. Do not start timers during render either. Verify repeated row selections interleaved with filtering/folding and refreshes; controls must keep responding and rows must reappear. Node-only model tests do not exercise this native ownership boundary.

For navigation, follow parent/dependent/child IDs, including a closed or filtered-out issue under folded ancestors. Verify that the target is highlighted and scrolled into view without inflating filter counts. Use Back/Forward across both list and relationship selections, then go Back and choose a new bead to confirm that Forward becomes unavailable. Re-selecting the current bead must not add a history step.

For persistence changes, use an isolated project to start a real session, remember a unique token, terminate the terminal, and resume. Verify the same session ID and worktree, token recall without resending it, preservation of unrelated metadata, live-session focus without duplication, and visible errors for missing transcripts or a different machine. Exercise both direct and Herdr launches and observe the dashboard's Start → Go to session → Resume transitions.

