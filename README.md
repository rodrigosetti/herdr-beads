# Beads in Herdr

User-level native Herdr mods for browsing Beads issues and managing issue-specific omp sessions through one local lifecycle controller. This source checkout lives at `~/.local/share/herdr-beads`, outside project repositories. Shipped Herdr layouts and mods are not modified.

## Install and open

Controller installation requires macOS launchd, `git`, `bd`, and Python 3 with `venv`/`pip`. Herdr execution additionally requires local Herdr, `hc`, `herdr-app`, and omp 18.17's worktree registry and extension APIs. GitHub PR verification requires `gh` and provider access; offline LOCAL work does not. Node is needed only for JavaScript regression tests.

```sh
bash ~/.local/share/herdr-beads/install.sh install --project /absolute/main-checkout
herdr-app do layout.switch --name beads-workspace
```

`install.sh` provisions the controller service and installs `beads-dashboard`, `bead-detail`, and `beads-sidebar` into `${HERDR_APP_HOME:-~/.config/herdr-app}/mods`. It installs the personal `beads-workspace` layout only when absent: an existing personal layout is preserved, so merge the sidebar integration deliberately if your custom layout does not already include it. `HERDR_APP_HOME` selects the Herdr configuration. New launches use the service-adjacent `bin/beads_omp_session.mjs`, never a standalone launcher or user-bin extension.

The installer writes absolute bridge/interpreter/config paths into the installed dashboard's `installation.js`; explicit pane props can override them for inspection. Source and generated extension/adapter/UI packaging remain owned here, not by Herdr or omp-core.

Open the project in a Herdr space and use its **Beads** sidebar button. All lifecycle actions go through the installed controller; service loss is an explicit refusal, not a local-launch fallback.

### Sidebar button

Switch to the personal layout once:

```sh
herdr-app do layout.switch --name beads-workspace
```

Click **Beads** above Settings in the sidebar. It opens a dashboard tab inside the currently selected space, using that space's first terminal's live folder. If the space already has a Beads dashboard, the button selects its existing tab and focuses the dashboard pane instead of creating a duplicate. The button remains available as an icon when the sidebar is collapsed and highlights while its dashboard tab is selected.

All dashboards for the same project use its canonical shared Beads database. Herdr remembers the selected layout across restarts; switching back to `main` restores the shipped interface without removing any tabs or sessions.


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

`stop` unloads only this user's service and retains its configuration/state; `restart` loads it again. Normal install and restart use scoped `launchctl bootout`, `bootstrap`, `kickstart`, and `print` in `gui/$(id -u)`. Failures are nonzero with diagnostics; only bootout's explicit “No such process”/missing-service result is accepted as already stopped. Startup must report service health within ten seconds. `--no-start` makes **no launchctl calls** and refuses a live singleton rather than mutating its installed package.

Defaults:

- Installed Python sources, schemas, and venv: `~/.local/share/herdr-beads-service` (outside repositories).
- Config: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/service.json`.
- Runtime health: the same directory's `service-status.json`; persistent store: `store`; socket: `controller.sock`.
- Logs: the same directory's `logs/stdout.log` and `logs/stderr.log`.
- LaunchAgent: `~/Library/LaunchAgents/com.herdr.beads.plist`.
- Stable machine identity: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/machine-id`, imported unchanged from `go-bd/machine-id` when present. Both files are retained; conflicting/invalid identities refuse startup.
- Cutover journal: the same state directory's `install-cutover.json`; retained per-install receipts and original retired executable bytes: `installations/<id>/`.

Service/config/store/log directories are owner-only; config, plist, and logs are 0600. The plist contains only explicit `HOME`, `USER`, `LOGNAME`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `HERDR_APP_HOME`, and a bounded executable-directory/system PATH; inherited `BEADS_DIR` and secrets are excluded. `status` prints config, health, log, socket, and store paths, optional capability gaps, launchctl state, and runtime health; launchctl or health errors remain visible and return nonzero. For direct launchd diagnostics:

```sh
/bin/launchctl print "gui/$(id -u)/com.herdr.beads"
```

For isolated provisioning, set absolute `HERDR_BEADS_INSTALL_ROOT`, `HERDR_BEADS_STATE_HOME` (the state-home base), `HERDR_BEADS_LAUNCH_AGENTS`, and `HERDR_BEADS_BIN_HOME` (normally `~/.local/bin`, used for owned legacy executable retirement); use the same overrides for subsequent management. These do not create a second label. **Use `--no-start` for isolation:** without it, management still targets the one normal per-user label. `HERDR_BEADS_PYTHON=/absolute/python3` selects the bootstrap interpreter; the venv uses its resolved real interpreter, not a pyenv shim.

The installed package includes the Python controller/adapters, schemas, UI bridge and `bin/beads_omp_session.mjs` together. Composition is static: `beads_omp.create_adapters(executables)` supplies the concrete execution/workspace adapters with the exact service socket. Controller policy is harness-neutral, but shipped execution is **omp-only** and depends separately on omp's worktree registry. Missing modules, registry bindings or tools refuse execution; they never select another harness.

On upgrade, rerun `install.sh install` with the same overrides and registrations, then reload existing dashboard tabs to load the installed mods. Do not run another controller against the same socket/store. Existing runs, transcripts, machine identity, immutable creation records and receipts remain retained. For existing history, use **Adopt existing session — preserve history** when the controller advertises it; Start never silently adopts. A live session's already-loaded extension is left untouched. Restarting an old terminal's immutable creation argv after extension retirement is unsupported: use controller/dashboard exact **Resume** to open the retained transcript in a new terminal with the renamed service-adjacent extension.

Installation preflights executable ownership, startup acknowledgements and locks before switching workflows. Unknown, symlinked or foreign legacy copies refuse visibly. A live legacy startup without its exact acknowledgement blocks retirement; an inactive failed startup may remain as historical evidence only after independent terminal inspection under the launch/controller locks. Nothing rewrites or deletes its failed receipt. Close only terminal-free legacy dashboard tabs before upgrading if their refresh owns `epics.lock`; keep all session tabs and workspaces. Normal install stops the old service before package changes, then health-checks the controller before activating mods. Original retired executable bytes and receipts remain archived. If cutover is incomplete, restart refuses: rerun install with the same settings to recover forward from its journal.

For troubleshooting, start with `install.sh status` and the printed service health/log paths. Check that Herdr/core, dashboard and service use the same `XDG_STATE_HOME` and `HERDR_APP_HOME`, that executable paths still exist after tool upgrades, and that omp recognizes the exact linked workspace in its own registry. Do not fabricate registry entries, reset the store/machine identity or delete receipts to clear a refusal. Provider outages hold PR proof without blocking offline LOCAL observation. A timeout leaves an inspectable operation, not permission to repeat terminal creation.

For isolated native smoke runs with long state-home paths, set a short `HC_SESSION` consistently for the manually started fixture app/core/service and CLI clients; macOS Unix sockets are path-length limited. The installed production LaunchAgent uses the normal per-user core session.

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

### Controller actions and lifecycle

The installed dashboard uses service-adjacent `bin/beads_ui.py` and controller-v1 for actions and lifecycle state. Default bridge/interpreter paths are `~/.local/share/herdr-beads-service/bin/beads_ui.py` and its `venv/bin/python`. Explicit `lifecycleBridge`, `lifecyclePython`, and `lifecycleConfig` props support isolated installations; `dashboardMod` identifies a separately named mod copy.

- **Actions** inspects an issue through controller status. Start, focus, exact resume, structured submission, integration verification, and explicit owner release follow controller-advertised availability/refusals. Only **omp** is supported; another agent appearing in Herdr is not a supported execution harness. Missing adapter capabilities remain unavailable, not a harness picker or launcher fallback.
- The destination is shown before starting. The default is explicit `local-target` on the canonical repository's `refs/heads/master`; **Configure explicit destination** edits a Target JSON object for new runs. `git config herdr.beads.target '<Target JSON>'` supplies a project default. A remote never selects PR mode. Local mode requires no PR, `gh`, authentication, or network; PR mode records provider host/stable repository ID/owner/name and full expected base ref.
- **Submit local handoff** / **Submit PR handoff** opens a reviewable structured form with the exact workspace HEAD and complete previously required submission set. PR submissions require the genuine PR number/URL. A handoff can explicitly change mode/target/submissions; it does not merge, push, or attest integration. The controller rejects stale fence/revision/coverage or mismatched source history.
- The dashboard has only **All open** and **Ready** filters; there is no Lifecycle tab or retained-run list. Per-issue **Actions** details expose canonical database/full issue ID, owner/run/generation, selected target, required submissions, mode-specific evidence, release and observed issue state. **Back to issues** returns to the current filter. A finished turn is never merge proof.
- Controller snapshots, operation errors, request IDs and operation IDs survive tab recreation, scoped to machine/database/canonical repository. Browsing and lifecycle failures are separate; failed refreshes keep last-good data visibly read-only. An uncertain request can be recovered with **Recover same request**, preserving its ID and payload; it is never silently replaced with another start. Service loss never launches locally.
- Complete verified coverage shows **Merged — awaiting manual closure**. The closure button **copies only** the controller's safely quoted normal `bd close --reason` command. Its limitation is explicit: normal manual Beads closure is not revision/run guarded. Herdr never executes/retries closure or workspace/registry/terminal deletion. External closure is an observation, not ownership proof; retained resources still require separate operator-reviewed manual cleanup. Reopen, reassignment or invalidated evidence suppress stale closure handoffs.
- Rewritten local history remains **Integration needs confirmation**, distinct from **Awaiting PR merge** and provider errors. The details explain the trusted local owner-receipt path; the socket/UI cannot attest it. See the controller receipt contract below.


## Start and return to work

Select an issue, open **Actions**, review the explicit destination, then use the controller-advertised Start, Focus or exact Resume action. Start checks readiness/inherited blockers and current ownership before claiming and preparing an omp workspace. Other owners' claims are not overwritten. An uncertain operation must be inspected or recovered with the same request, not replaced by another launch.

Workspaces use full issue identity; their Herdr space display name is the current Beads title. Duplicate titles are allowed: identity comes from canonical repository/database, run, workspace and session bindings, never the display label. Title changes do not move workspaces, rename branches or replace saved conversations.

Each launch pins `BEADS_DIR` to the canonical shared database. The execution adapter validates exact durable session identity; the workspace adapter independently validates omp registry/source, Git common directory and branch. Supported workspaces are **linked Git worktrees only**. On omp 18.17 independent clones lack a supported registry binding and are retained/refused, not accepted via clone-origin inference or relaxed validation.

### Epics are containers

Epics offer no Start or Resume action and cannot be claimed as executable sessions. Start a concrete child task instead, including planning and investigation work. Existing historical epic associations remain retained for inspection.

The service's `bin/beads_epics.py` observer supplies read-only direct-child progress. Only `closed` children count; actual parent edges determine membership, not dotted IDs. Nonempty all-closed epics report eligibility for **owner review**, never automatic closure. Empty epics are not eligible; nested progress does not invent transitive closure.

Registered sessionless projects and projects discovered from durable local runs are observed at startup, periodic passes and validated-request wakeups. Per-project/per-epic errors remain visible without blocking browsing. No refresh/service operation closes, reopens or repairs an epic.

Child counts and manual Beads closure are not one atomic transaction. Owners review current child state and normal Beads close gates separately; no force, raw SQL, or background repair bypass is used.

### Session association

Controller-created terminals carry canonical `beads.project` and `beads.issue` labels, but labels alone do not authorize adoption or execution. Existing sessions require the explicit validated legacy-adoption boundary described below; neither titles nor a manually applied label establish ownership.

### Persistent conversations

The controller loads the service-adjacent startup extension with `BEADS_OMP_SESSION_CONFIG`. It obtains the actual persistent session ID and transcript path from omp's lifecycle API and publishes a durable fenced binding to the controller. The execution adapter alone owns the field-preserving `metadata.omp_session` mirror: exact `session_id`, absolute `session_file`, stable local `machine` and original `worktree`.

A fresh task requires exact-session acknowledgement after validation and metadata publication. The extension makes no Beads mutations. Subagents cannot replace the primary association. Switch, fork/branch and tree navigation are refused while mapped: they require a new controller binding, not an implicit conversation replacement.

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

Journaled operations and owner-only session configurations live under `${XDG_STATE_HOME:-~/.local/state}/herdr-beads`. Scope and worker locks serialize effects; concurrent or uncertain launches are refused/held rather than duplicated. A failed launch preserves its claim, workspace, terminal, transcript and receipts for inspection. Slow operations may exceed the client deadline: inspect status or recover the same request before acting again.

### Manual completion policy

The lifecycle controller verifies integration and prepares an evidence-backed owner handoff: **Merged — awaiting manual closure**. Both offline LOCAL and genuine GitHub PR workflows are supported with separate authority; neither a finished turn nor local integration proves remote publication. Beads/Dolt remote synchronization is a separate operator-authorized action.

The owner reviews the current canonical database, full issue ID, ownership/run/submission, target, and merge evidence, then separately executes normal `bd close --reason …` without `--force`. Manual closure retains Beads' existing concurrency limitations: it is not atomically guarded against reopen, reassignment, or a newer run. A Herdr lock or a preceding status check does not remove that race. Observing an externally closed issue is not proof that a particular run owned its closure.

Automatic Bead closure, closure retries, workspace deletion, pruning, and destructive registration/space/terminal/transcript cleanup remain disabled, including after verified merge, manual closure or explicit session release. Explicit release relinquishes session ownership; it does not establish integration, Beads closure or cleanup authorization. Observed external closure is only a timestamped issue observation, not a run-owned closure receipt. Workspaces, registrations, sessions and transcripts remain retained and inspectable after the Bead closes and across service restarts; restart reconciliation does not delete them.

Workspace observation is also not cleanup proof. `missing` records observed absence without removing the canonical workspace identity, provider registration or other retained resources. `unknown` means observation is unavailable or inconclusive (including unsupported provider capabilities) and requires actionable operator review, not a guessed absence. An operator may separately review current ownership/activity, tracked, untracked and ignored work, stashes, nested repositories and unpreserved commits, then remove resources externally under the ordinary provider workflow. The controller, dashboard and session context provide no destructive cleanup command or button. Guarded automation is optional backlog (`herdr-beads-6yd`), not a release prerequisite; no upstream Beads patch or database topology change is required for the manual-completion scope.

### Explicit `/land` command

In an omp session whose checkout contains `.omp/commands/land.md`, invoke `/land` to authorize completion of that session's task: relevant checks and smoke verification, a task-only worktree-branch commit, merge into local `main`, ordinary Beads closure, then removal of the exact linked worktree and merged branch. The command is project-scoped; it does not install itself globally or into existing worktrees. Start a new session or use `/reload-plugins` after adding the file to the current checkout.

This is a reusable agent prompt, not a transactional cleanup tool or a change to lifecycle-controller automation. Each invocation grants explicit permission for that task only, including automatic merge-conflict resolution that preserves the task's intent and unrelated changes on `main`, with quality gates and smoke verification repeated on the resolved tree before completing the merge. It forbids push, fetch, pull, Dolt remote sync, forced removal, and unrelated cleanup; conflicts that cannot be resolved safely from repository evidence, failed checks, ambiguous identity, ownership changes, or unsafe cleanup stop the operation with a precise partial-completion report. Bound sessions must satisfy their existing release/relocation requirements before removal. Transcripts and unrelated work are preserved. Closing a session does not invoke `/land`.

## Durable local controller

The version-1 controller includes a private JSON run store, concrete omp execution/workspace adapters, read-only integration observer, bounded Unix-socket request CLI, controller-connected omp startup extension and installed launchd scheduler. The installed dashboard uses these controller actions. Legacy history is adopted explicitly, never silently migrated. Policy is harness-neutral; shipped execution supports only omp and refuses missing capabilities without an alternate harness or standalone fallback.

```sh
# With the launchd service installed/running: one request in, one response out.
~/.local/share/herdr-beads-service/venv/bin/python \
  ~/.local/share/herdr-beads-service/bin/herdr-beads request < request.json
```

`request --socket /absolute/path/controller.sock` selects an isolated socket; default: `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/controller.sock`. The socket parent must be owner-only (0700); the socket is 0600. The service accepts only local same-user peers on macOS/Linux and only versioned, validated run-scoped operations—not arbitrary commands or remote execution. Request/response limits are 1 MiB / 4 MiB, with a five-second client deadline. Exit codes: 0 for `ok`/`accepted`, 2 for `refused`, 1 for transport/dependency/IO errors. A timeout/disconnect is **not cancellation**: reuse the same request ID/payload or inspect status before acting. `bin/herdr-beads serve` is the low-level injectable transport, not the installed periodic scheduler; do not run competing owners on the installed socket.

Each socket message is one newline-terminated JSON object. Clients send the request and read the response without a write-side shutdown: the server may respond and close immediately, and on Darwin a subsequent `SHUT_WR` can fail with `ENOTCONN` even while the complete response remains buffered. Requests are not automatically retried.

The controller retains the existing machine UUID in `herdr-beads/machine-id`. Import uses fsynced, exclusive atomic publication; it never replaces an existing identity. Both canonical and historical files are checked while present; a missing/invalid established identity or disagreement refuses startup. Only a genuinely fresh service install may create a UUID. Repository identity binds canonical checkout, Git common directory, independently resolved canonical Beads directory, full issue ID and machine. Linked worktrees resolve to the canonical source checkout. Independent clones require provider registry/source validation unavailable through omp 18.17's public API; they remain unsupported and retained/refused, never rebound by guessing origin.

### Versioned requests and independent state

The complete wire shapes live in `schemas/v1/{controller,run,adapter}.schema.json`. Every request has `apiVersion:1`, a unique `requestId`, canonical `scope`, `method`, and typed `params`.

The durable version-1 `Cleanup` record adds optional `reason` (explicit automatic-cleanup-disabled retention prose) and `workspaceObservation: {state, reason, observedAt}` fields. Observation state is `present`, `missing` or `unknown`, its reason is nonempty review context, and `observedAt` is a date-time. Older stored records without these fields remain valid; surfaces show unknown/no recorded observation until observation is available, not fabricated historical evidence. These additive fields do not replace `run.workspace`: its exact canonical provider, backing, path, branch, source and common directory remain authoritative alongside `run.scope`, owner and fence. Saved observations survive restart but are timestamped snapshots, not fresh ownership/activity checks or a comprehensive inventory of work to preserve. Retained closed runs remain available through controller status independently of open-issue lists, without a dashboard inventory tab. Issue Actions details and `/bead-context` expose the retention reason and workspace observation without granting cleanup authority.

For example, status has no mutation precondition:

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

`bin/beads_omp.py:OmpExecution`, `bin/beads_workspace.py:OmpWorkspace`, and `bin/beads_herdr.py:HerdrHosting` are separate concrete dependencies, not a plugin framework. `create_adapters(executables)` supplies static service composition with explicit absolute executable paths; null tools never fall back to ambient PATH. Launch, writable/read-only exact resume, durable identity, owner activity, structured handoff, read-only enforcement and explicit release are supported. Control acknowledgements require the exact durable request, fence, binding, session, live PID, held lease and frozen extension gate. Recovery observes the retained request/receipt without rewriting or relaunching it.

Before terminal creation, the adapter fsyncs an owner-only per-run/generation startup config and initial task outside the workspace under `${XDG_STATE_HOME:-~/.local/state}/herdr-beads/omp`. New immutable creation argv carries `BEADS_OMP_SESSION_CONFIG`. Interrupted/slow requests reconnect by that exact config identity, including the creation-to-labeling gap, without another terminal creation. Recovery of an already-journaled historical creation may inspect its immutable `GO_BD_SESSION_CONFIG`; it is never supplied to a new launch. Unconfirmed operations retain all resources for inspection.

Start/focus/resume recheck the canonical database, owner, blockers and workspace identity. Focus requires the acknowledged foreground omp PID and exact transcript lease, not a title or stale agent report. Ended terminals resume only the recorded absolute transcript with `--resume`, no fresh task or `--restore`; missing, foreign, conflicting or leased history refuses intact. Existing legacy associations require explicit adoption rather than a new-run overwrite.

### Legacy adoption

Send `method:"adopt_legacy", params:{"expected":null}` with the same canonical scope as `status`. Adoption records intent before inspecting legacy state; restart repeats read-only reconciliation rather than launching. Completion retains the concrete omp evidence privately and records the neutral session/workspace result before any later cutover. No legacy JSON, transcript, lease, Beads metadata, branch, registry or machine-ID file is retired or rewritten. The legacy issue lock serializes observation with an in-flight old launch.

Sources are the exact `metadata.omp_session`, per-database `go-bd/<sha256(beadsDir)>/issue-<full-id>.json`, original launch config/ack, terminal creation argv, canonical `beads.project`/`beads.issue` labels, foreground omp PID, exact transcript lease, and independently validated omp workspace registry/Git backing. A live terminal requires agreement among these identities, not merely generic Herdr labels, a title or a recent transcript. Conflicting, malformed, foreign-machine, missing or orphaned resources refuse visibly and remain retained for explicit resolution. Malformed launch files in the project's legacy state conservatively block adoption even when their issue cannot be determined.

An adopted live run focuses its existing terminal; repeating adoption focuses the same incarnation. An ended, unleased exact conversation is adopted stopped, without creating a terminal. A subsequent `resume` or `start_or_focus` reopens only the original transcript/workspace, without a fresh prompt. Repeated `adopt_legacy` on a retained inactive run uses that same exact-resume path. Original branch names and validated Git backing are preserved; new workspace planning keeps its stricter issue/attempt naming checks. Independent-clone adoption is unsupported on omp 18.17; an unbound clone refuses intact rather than being converted or registered by this integration.

Adopted runs start with `target:null`, no submissions/evidence, held release and `manual_cleanup_required`. Use `target:null` and the retained policy when focusing/resuming an unbound run with `start_or_focus`; null is refused for new attempts. Explicit `handoff` supplies mode/target and exact required submissions. Neither a remote nor a branch name chooses local-target versus PR mode. Adoption cannot produce merge proof, a manual closure command or cleanup permission.

The dashboard preserves an adopted run's null destination even when a project default is configured. Its issue Actions panel shows the unbound target without assuming a mode; the handoff editor leaves destination fields unbound for explicit owner input before controller validation.

Observed adoption validation (2026-11-09): two actual CLI/socket requests adopted/focused the running omp conversation `01a1c269-5da9-710c-a5b4-7f86278ef4b9` in terminal `t2n` using an isolated controller store. Transcript bytes, legacy JSON and the terminal set were unchanged; canonical linked backing and original branch were validated, and destination remained null. This is live adoption proof, not installed cutover or managed-PR integration acceptance.


Workspace planning preserves the explicit integration target and branches new work from its already-local ref; no remote, fetch, push, PR creation or merge is required for LOCAL mode. Registry/source/common-dir/branch checks reject unsupported independent clones; clone origin never selects the integration destination. Omp 18.17's copy-on-write “clone” provisioning still creates linked Git worktrees and is not independent-clone acceptance. Supported config queries can initialize omp settings storage, and `omp worktree add` runs existing checkout hooks; the adapter itself issues no network or integration commands.

**Approved backing scope (2026-11-10, `herdr-beads-rlm.5`): linked Git workspaces only for this cutover.** Independent clones are unsupported on omp 18.17 and no longer a cutover acceptance prerequisite; this is an explicit owner scope decision, not successful clone acceptance. Both offline/no-remote LOCAL and genuine GitHub PR acceptance remain mandatory, including representative installed proof after legacy-path removal. Preserve all registry/source/branch/common-dir, session and ownership protections. An unbound clone remains refused and retained; no registry forgery, origin-based binding, alternate provider, upstream patch or resource removal is authorized.


### Omp session extension

`bin/beads_omp_session.mjs` is owned and packaged by **herdr-beads**, not omp-core. `install.sh` installs it only beside the execution adapter at `${HERDR_BEADS_INSTALL_ROOT:-~/.local/share/herdr-beads-service}/bin/beads_omp_session.mjs`. The adapter explicitly loads that adjacent file with `omp --extension /absolute/path/beads_omp_session.mjs`; no user-bin copy, ambient discovery or compatibility launcher is provided. Its API baseline is [omp 18.17.0](https://github.com/can1357/oh-my-pi/blob/v18.17.0/docs/extensions.md): durable storage/leases, main/sub identity, lifecycle hooks, managed timers, async-job snapshots and registered commands.

Controller-v1 configuration and acknowledgement are mandatory; missing/refused startup identity exits before startup work. Historical launch files are evidence only. `GO_BD_SESSION_CONFIG` may be read solely at explicit legacy adoption or recovery of an already-journaled immutable historical creation; new launches always supply `BEADS_OMP_SESSION_CONFIG`.

`BEADS_OMP_SESSION_CONFIG` names an absolute, owner-only JSON file in an owner-only directory outside the workspace. The adapter supplies:

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

Historical extension evidence (before controller-only cutover): actual omp 18.17.0 TUI context/handoff/release commands exercised a local protocol peer, persistent transcript/lease handling and delayed six-second acknowledgement. Earlier metadata-mirror validation preceded its transfer to the execution adapter. This is adapter-surface evidence only, not post-removal installed proof.


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

### Historical pre-cutover evidence

Earlier isolated controller/observer smoke exercised socket recovery/concurrency refusals, no-remote local normal/fast-forward integration, trusted squash receipts and reset invalidation, plus genuine read-only provider observations for [merge](https://github.com/kubernetes/kubernetes/pull/143983), [squash](https://github.com/python/cpython/pull/161674), [rebase](https://github.com/zulip/zulip/pull/40635) and [cli/cli#8294](https://github.com/cli/cli/pull/8294). Herdr start/focus/exact resume retained transcripts, token recall and metadata. Disposable manual closure/reopen and service restart retained resources; externally missing or unregistered workspaces remained held, not cleaned up.

Source-dashboard smoke used an isolated controller and separately named native mod, including [NVIDIA/OpenShell#5077](https://github.com/NVIDIA/OpenShell/pull/5077) read-only evidence. It exercised structured handoff, copy-only closure, tab recreation and independent local/provider holds. Initial-render checks and source screenshots are historical surface evidence, not production pointer-dispatch or post-removal acceptance. A later retention screenshot attempt returned a stale frame/black capture and supplies no visual proof.

These historical observations do not assert current test totals, independent-clone support, or installed controller-only cutover proof. The installed disposable evidence below is retained with its original scope; final post-removal evidence is separate.

Retention smoke (2026-11-10) used two isolated real Beads databases and the actual socket service: offline local-target verification and genuine read-only [cli/cli#8294](https://github.com/cli/cli/pull/8294) evidence both reached manual handoff. Separate fixture-owner closure removed the issues from open lists; service restart retained directories, Git registrations and byte-identical transcripts. An external rename produced `missing`/`held`, preserving the exact registration and external-closure observation; unregistered workspaces reported actionable `unknown`, not cleanup success. Deterministic service regressions cover linked and independent-clone identity policy, explicit release, dirty/untracked/ignored work, stashes, nested repositories and changed identity. Clone regressions substitute the provider registry boundary and are not real independent-clone acceptance.

All three source native mods passed initial-render checks. The source retention-pane screenshot was stale and desktop capture was black; neither supplies visual or installed-cutover proof.

### Historical installed disposable acceptance — 2026-11-10

`herdr-beads-rlm.3` exercised separately provisioned installed service packages (`--no-start`), isolated real Beads databases, actual Herdr windows/cores and omp 18.17.0. App, service and core used matching `XDG_STATE_HOME`/`HERDR_APP_HOME`; mismatched environments correctly refused startup rather than adopting another terminal. Production installations, issues, PRs and workspaces were not used for destructive proof. No source commit, git push, Dolt remote sync, workspace removal or automatic closure occurred.

| Track | Exercised result | Retained evidence |
|---|---|---|
| LOCAL, linked | No remotes; canonical `refs/heads/master`; configured `gh` unavailable. Real start/focus, exact writable/read-only resume, token recall, structured handoff, normal/fast-forward integration, missing/trusted squash receipt, target-reset invalidation, concrete read-only/release acknowledgement, app-closed ordinary owner closure, SIGKILL/service recovery, reopened/reassigned ownership and extra-work retention. | `/Users/rodrigosetti/.local/state/hr3-b62senr0/local-evidence-summary.json`; native retained-run screenshot `local-retained-inspection.png`. |
| PR, linked | Genuine new private [disposable repository](https://github.com/rodrigosetti/herdr-manual-pr-20261110-172452-8ef3b2), stable repository ID `1452974756`, base `refs/heads/main`. Real omp start/focus/resume/token recall, full required PR coverage including merge/squash/rebase, handoff/release, app-closed ordinary owner closure and restart retention passed. Wrong head/base, closed-unmerged, per-process HTTP 401 and refused HTTPS-proxy failures withheld proof/commands. | `/private/tmp/herdr-real-pr-ce4e8bcac7/pr-evidence-summary.json`, `stacked-merged-status.json`, `manual-close-results.json`, `final-restart-statuses.json`, `retained-resource-evidence.json`. |
| Independent clone | **Unsupported; excluded by explicit owner decision in `herdr-beads-rlm.5`, not accepted.** A genuine no-remote clone was retained and visibly refused before launch: actual `omp worktree list --json` classified it as an unbound stray; controller returned `workspace_mismatch`. Linked copy-on-write provisioning is not independent-clone backing. | LOCAL fixture `clone-real-provider-refusal.json` remains retained; clone proof is no longer a cutover prerequisite. |

Exact LOCAL identity: `rlmlocal-whu`, run `da65d791-1f59-4747-9f6a-bd93b0b97e9c`, integration generation 2, session `01a1c680-025f-70c9-a4cc-dfd65108402b`, submitted `cab1115d32e7575403e5981c9ee1ffc806c1b629`; normal result `5a24d341b39fb29901f0aec31e1a8caa814ae156`, reviewed squash result `d9042b27b3afed3d776a468dec4ba164782cb6b5`. The trusted receipt was independently verified; the socket refused owner attestation. Separate fast-forward fixture `rlmlocal-z0d` integrated `e3c01f0a2162bf48c5616d47efd91e6b2e67b2f7` and retained its exact conversation through explicit read-only resume before release/closure.

Exact PR identity: `prfixture-dgm`, run `f747b0a6-3e0b-483c-8f8f-d1f9c8357787`, generation 2, session `01a1c681-2caf-77fd-8df2-7cfeafe79b31`, [PR 5](https://github.com/rodrigosetti/herdr-manual-pr-20261110-172452-8ef3b2/pull/5), head `1d08a9fc2b24f6240e93cd989a58a719840863d3`, result `912dc265eead335391d2e0cd8e297ceb928d67f0`. Stacked run `2bd4dae0-80b3-4bdb-babc-1ab325125ea2` required genuine PRs 1/2/3/6 simultaneously, including exact HEAD `e835b6a9ce537c49fab600c6238e2113361710f1`; all integrated. These are managed disposable-project proofs, not unrelated public merged responses.

Real failure/recovery exposed and fixed missing concrete release/read-only support, loss of visible extra-work diagnostics after external closure, and invalid released activity during reopen/reassignment. Closed retained runs now expose `dirty` and `unpushed_or_unpreserved` blockers plus current/submitted HEAD diagnostics; released sessions remain inactive after ownership revocation. Missing `omp` visibly refused launch, exact resume and release; unsupported harness and cleanup requests also refused without substitutes or removal. Ordinary manual closure remains **not revision/run guarded**; no atomic stale-state rejection is claimed.

Historical validation at that stage: the full Python suite passed 279 tests and Node passed 59 tests; after the released-revocation correction, all 6 affected retention tests passed. Then-existing launcher/model regressions were retained at that stage; these are not current post-removal test totals. Final installed observations retained released/inactive sessions, an externally closed PR fixture and an externally reopened/reassigned fixture without a closure command. Recovery did not erase earlier failed-operation journal/health diagnostics.

The native dashboard rendered closed retained resources and their blockers separately from open issues; actual omp rendered context/handoff/release acknowledgements. This historical acceptance predates the installed controller-only cutover and is not post-removal proof or exhaustive pointer dispatch. Machine-wide sleep, production app quit, real account credential revocation and router/firewall failure were not exercised; only isolated apps were quit and outages were per-process. The [omp 18.17 scanner](https://github.com/can1357/oh-my-pi/blob/v18.17.0/packages/coding-agent/src/cli/worktree-cli.ts) supplies no public independent-clone registration command. The initial implementation handoff retained clone acceptance as a blocker; the subsequent explicit linked-only decision in `herdr-beads-rlm.5` removed that prerequisite without asserting clone acceptance. Both linked LOCAL/PR tracks and final installed post-removal proof remained required. No registry forgery or relaxed identity checks are authorized.



### Controller-only installed cutover — 2026-11-10

Production installation now runs `com.herdr.beads` with the controller-backed mods and concrete renamed omp extension. Owned user-bin launcher/extension/epic-helper copies are absent; their ownership hashes and original bytes are retained in `~/.local/state/herdr-beads/install-cutover.json` and its installation receipts. All 99 historical legacy state files and both recorded transcript prefixes remained unchanged. The already-running `.4` session was adopted/focused in terminal `t3f` without relaunch or destination inference; production screenshot: `/Users/rodrigosetti/.local/state/rlm4-n591qmf8/production-controller.png`.

Final retained evidence: `/Users/rodrigosetti/.local/state/rlm4-n591qmf8/post-retirement-evidence.json` and `controller-requests.json`. Both linked-only tracks exercised installed sidebar Start, focus without duplication, exact resume/token recall, package upgrade with durable run/session preservation, another exact resume, structured extension handoff, verified integration/read-only acknowledgement, native copy-only closure command, explicit release, ordinary external fixture-owner closure, app-closed service restart, observed closure and retained registered workspace/transcript/terminal resources.

| Track | Exact post-removal identity and proof |
|---|---|
| LOCAL | `cutlocal-48i`; run `804eecbd-9a11-4a52-9ac8-b08946f1bebb`, generation 3; session `01a1c6ba-4bbc-77e5-885e-dfdbbdd08def`. No remotes and configured `gh:null`; canonical `refs/heads/master` independently verifies existing fast-forward source/target `e3c01f0a2162bf48c5616d47efd91e6b2e67b2f7`. |
| GitHub PR | `cutpr-d2w`; run `dbecedf4-736d-4ede-9989-00d6064284a3`, generation 3; session `01a1c6ba-4a3f-7707-b21f-68f7db4bcb59`. Genuine fresh provider observation of [fixture PR 5](https://github.com/rodrigosetti/herdr-manual-pr-20261110-172452-8ef3b2/pull/5), repository ID `1452974756`, base `refs/heads/main`, head `1d08a9fc2b24f6240e93cd989a58a719840863d3`, merge result `912dc265eead335391d2e0cd8e297ceb928d67f0`. |

This representative repeat reverified earlier genuine integrated revisions; it performed no new merge, fetch, push or publication. Both runs ended `observed_closed`, `released/inactive`, workspace observation `present`, cleanup `held/manual_cleanup_required`, with no closure command. Normal manual closure was executed only by the fixture-owner terminal outside integration code. Native handoff screenshots live in each track's `verified-manual-handoff.png`; bounded UI timeouts stayed visible and same-request recovery was exercised. Pointer dispatch, machine sleep and production app quit were not tested. Initial stale startup frames are not visual acceptance evidence.

Validation: 260 lifecycle/adapter/service Python tests plus 43 installer tests passed; 61 Node tests passed. All three installed native mods passed checks in production and both fixtures. Only disposable observer processes/apps were stopped after recovery proof; their resources remain retained. Production service, live conversation and `.4` issue remained active at that handoff. The original installed payload came from the dedicated `.4` worktree; owner-directed local integration preserves that tested runtime code and makes the cutover available from canonical `main`.

## Verification

```sh
cd ~/.local/share/herdr-beads
herdr-app check ~/.config/herdr-app/mods/beads-dashboard
herdr-app check ~/.config/herdr-app/mods/bead-detail
herdr-app check ~/.config/herdr-app/mods/beads-sidebar
node --test model.test.mjs lifecycle.test.mjs session.test.mjs
python3 -m unittest epic_test
python3 -m unittest discover -p '*_test.py'
```

The native check only covers the initial render. Also open the dashboard in Herdr, switch between All open and Ready, check Ready against the union of `bd ready --limit 0` and `in_progress` issues, and select two issues to confirm detail-pane reuse and formatted Markdown. Check that blocked, in-progress, and deferred issues remain available in All open and closed issues do not appear as matches. For changes to session launching, also exercise a real worktree agent. Session attention states must be observed in the running app, not inferred from an issue's `in_progress` field.

Use actual pointer clicks for this check, including the first click after focusing another pane. Dashboard background focus is handled on click, not mouse-down, to avoid focus-driven redraws during a pending control click. Issue rows stop propagation so selecting a bead does not refocus the dashboard after opening its details. Mod commands alone do not verify pointer dispatch.

For list-rendering changes, verify a loaded viewport in the running app, not just an initial-render check: rows must remain visible after refresh and resizing, disclosure arrows must fold/unfold children, and selection/session buttons must still work. The virtual-list callback must only mount retained row views; row handlers belong to those views, and row actions/reconciliation run in the dashboard task. The list height is measured from its viewport.

The action queue drains in a dashboard-owned native timer callback every 50 ms; idle ticks do not notify or redraw. Do not replace it with a JavaScript promise wake, even with `cx.sleep()`: a promise resolved by a row callback can retain that row's native ownership scope. Filtering/folding that row can then release sibling entities and kill the action loop on its next update. Do not start timers during render either. Verify repeated row selections interleaved with filtering/folding and refreshes; controls must keep responding and rows must reappear. Node-only model tests do not exercise this native ownership boundary.

For navigation, follow parent/dependent/child IDs, including a closed or filtered-out issue under folded ancestors. Verify that the target is highlighted and scrolled into view without inflating filter counts. Use Back/Forward across both list and relationship selections, then go Back and choose a new bead to confirm that Forward becomes unavailable. Re-selecting the current bead must not add a history step.

For persistence changes, use an isolated project to start a real session, remember a unique token, terminate the terminal and exact-resume through controller actions. Verify the same session ID/worktree, token recall without resending it, preservation of unrelated metadata, focus without duplication and explicit missing-transcript/foreign-machine refusals. Exercise installed dashboard and session extension with both offline LOCAL and genuine GitHub PR tracks; neither track substitutes for the other.

