import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:net";
import { once } from "node:events";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, realpathSync, openSync, fsyncSync, closeSync, rmSync, chmodSync } from "node:fs";
import { join } from "node:path";
import sessionExtension from "./bin/go-bd-session.mjs";

const clone = value => JSON.parse(JSON.stringify(value));
const readJSON = path => JSON.parse(readFileSync(path, "utf8"));
const quietJobs = () => ({ running: [], recent: [], delivery: { queued: 0, delivering: false, pendingJobIds: [] } });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
class ExitRequested extends Error {
  constructor(code) { super(`process.exit(${code})`); this.code = code; }
}

// Only documented omp APIs are provided. Timers are driven explicitly so no
// polling, wall-clock sleeps, or live extension timers cross test boundaries.
async function fixture(t, options = {}) {
  const root = realpathSync(mkdtempSync("/tmp/gbds-"));
  const worktree = join(root, "workspace");
  mkdirSync(worktree, { mode: 0o700 });
  const configPath = join(root, "config.json");
  const transcript = join(root, "session.jsonl");
  const prompt = join(root, "task.txt");
  const controllerPath = join(root, "controller.json");
  const config = {
    schemaVersion: 1,
    scope: { repository: { root: worktree, commonDir: join(worktree, ".git") }, beadsDir: join(root, ".beads"), issueId: "test-1", machineId: "machine-1" },
    fence: { runId: "run-1", attempt: 1, generation: 1 }, bindingId: "binding-1", credential: "private-test-credential",
    controllerSocket: join(root, "controller.sock"), worktree, previous: null,
    ack: join(root, "ack.json"), prompt_file: prompt,
    terminal: { machineId: "machine-1", address: "terminal-1", spaceId: null, tabId: null }, access: "writable",
  };
  const identity = { session_id: "primary-session", session_file: transcript, machine: config.scope.machineId, worktree };
  if (options.previous) config.previous = { ...identity, ...options.previous };
  writeFileSync(configPath, JSON.stringify(config), { mode: 0o600 });
  writeFileSync(prompt, "Implement the assigned Bead, without closing it.\n", { mode: 0o600 });
  const header = { type: "session", version: 3, id: identity.session_id, cwd: worktree, ...options.header };
  function writeTranscript() {
    const fd = openSync(transcript, "w", 0o600);
    try { writeFileSync(fd, JSON.stringify(header) + "\n"); fsyncSync(fd); }
    finally { closeSync(fd); }
  }
  writeTranscript();

  const oldConfig = process.env.GO_BD_SESSION_CONFIG;
  process.env.GO_BD_SESSION_CONFIG = configPath;
  const exit = t.mock.method(process, "exit", code => { throw new ExitRequested(code); });
  const stderr = t.mock.method(console, "error", () => {});
  const requests = [];
  const errors = [];
  const sockets = new Set();
  const target = { mode: "local-target", repository: clone(config.scope.repository), ref: "refs/heads/main" };
  const now = "2026-01-01T00:00:00.000Z";
  const controller = {
    beforeResponse: null,
    run: {
      schemaVersion: 1, fence: clone(config.fence), revision: 1, scope: clone(config.scope), owner: "owner", disposition: "current", harness: "omp",
      policy: { closeAfterIntegration: false, cleanupAfterRelease: false }, target,
      workspace: { provider: "omp-worktree", backing: "linked", path: worktree, branchRef: "refs/heads/test-1", sourceRepository: clone(config.scope.repository), commonDir: config.scope.repository.commonDir },
      execution: { state: "starting", access: "writable", activity: "unknown", session: null, terminal: null, observedAt: null, readOnlyAcknowledged: false },
      coverage: { revision: 0, workspaceHead: null, required: [], state: "unsubmitted" }, submissions: [],
      release: { state: "held", receipt: null }, closure: { state: "not_requested", evidenceIds: [], observedAt: null, reason: null, manualCommand: null },
      cleanup: { state: "held", blockers: ["manual_cleanup_required"], checkedAt: null }, issueObservation: null, createdAt: now, updatedAt: now,
    },
  };
  const saveController = () => writeFileSync(controllerPath, JSON.stringify(controller.run), { mode: 0o600 });
  saveController();
  const server = createServer({ allowHalfOpen: true }, socket => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => {});
    let input = "";
    let handled = false;
    socket.on("data", chunk => {
      input += chunk.toString("utf8");
      if (handled || !input.includes("\n")) return;
      handled = true;
      void (async () => {
        const request = JSON.parse(input.trim());
        requests.push(request);
        assert.equal(request.apiVersion, 1);
        assert.deepEqual(request.scope, config.scope);
        const run = controller.run;
        let action;
        if (request.method === "status") {
          assert.deepEqual(request.params, { runId: null });
          action = "status";
        } else if (request.method === "observe") {
          const { credential, observation } = request.params;
          assert.equal(credential, config.credential);
          const durable = readJSON(`${configPath}.binding.json`);
          assert.deepEqual(durable.observation, observation, "controller must see the durable receipt before observing it");
          assert.deepEqual(durable.fence, config.fence);
          assert.equal(durable.machineId, config.scope.machineId);
          assert.equal(durable.workspacePath, worktree);
          const event = observation.event;
          if (event.kind === "session_bound") {
            assert.deepEqual(durable.omp, identity);
            assert.equal(durable.pid, process.pid);
            assert.equal(readJSONLines(transcript)[0].id, identity.session_id);
            run.execution.session = clone(event.session);
            run.execution.terminal = clone(event.terminal);
            run.execution.state = "running";
            run.execution.activity = event.activity;
          } else if (event.kind === "activity") {
            run.execution.activity = event.activity;
            run.execution.state = event.turn === "idle" ? "idle" : "running";
          } else if (event.kind === "exited") {
            run.execution.state = "stopped";
            run.execution.activity = event.activity;
          } else if (event.kind === "release_acknowledged") {
            assert.equal(run.release.state, "requested");
            assert.equal(durable.frozen, true);
            run.release = { state: "released", receipt: clone(event.receipt) };
            run.execution.state = "released";
            run.execution.activity = "inactive";
          } else if (event.kind === "read_only_acknowledged") {
            run.execution.access = "read_only";
            run.execution.readOnlyAcknowledged = true;
          } else {
            assert.fail(`unexpected observation: ${event.kind}`);
          }
          run.execution.observedAt = observation.observedAt;
          run.revision++;
          action = "observation_recorded";
        } else if (request.method === "release") {
          assert.deepEqual(request.params.expected, { fence: run.fence, revision: run.revision });
          assert.equal(readJSON(`${configPath}.binding.json`).frozen, true, "owner gate must close before release reaches controller");
          run.release.state = "requested";
          run.revision++;
          action = "release_requested";
        } else {
          assert.fail(`unexpected controller mutation: ${request.method}`);
        }
        saveController();
        let response = {
          apiVersion: 1, requestId: request.requestId, outcome: "ok", action, run: clone(run), operation: null, actions: [],
          support: { harnesses: ["omp"], execution: { harness: "omp", supported: ["launch", "exact_resume", "durable_identity", "owner_activity", "structured_handoff", "explicit_release", "read_only"], unsupported: [] }, workspaceProvider: "omp-worktree", integrationModes: ["local-target", "github-pr", "remote-target"] },
        };
        if (controller.beforeResponse) response = await controller.beforeResponse(request, response);
        socket.end(JSON.stringify(response) + "\n");
      })().catch(error => {
        errors.push(error);
        socket.end(JSON.stringify({ apiVersion: 1, requestId: "fixture-error", outcome: "error", error: { message: error.message } }) + "\n");
      });
    });
  });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    if (server.listening) await new Promise(resolve => server.close(resolve));
    if (oldConfig === undefined) delete process.env.GO_BD_SESSION_CONFIG;
    else process.env.GO_BD_SESSION_CONFIG = oldConfig;
    exit.mock.restore();
    stderr.mock.restore();
    rmSync(root, { recursive: true, force: true });
    assert.deepEqual(errors, [], "local controller assertions must succeed");
  });
  server.listen(config.controllerSocket);
  await once(server, "listening");
  chmodSync(config.controllerSocket, 0o600);

  const leases = { claims: [], releases: 0, held: false };
  function client(kind = "main", sessionId = identity.session_id, sessionFile = transcript) {
    const hooks = new Map();
    const commands = new Map();
    const intervals = [];
    const timeouts = [];
    const messages = [];
    const notices = [];
    const session = { id: sessionId, file: sessionFile, ensureCalls: 0 };
    const activity = { streaming: false, pending: false, snapshot: quietJobs() };
    const ctx = {
      agent: { kind }, cwd: worktree,
      sessionManager: {
        getSessionId: () => session.id, getSessionFile: () => session.file,
        getCwd: () => worktree, getRecordedCwd: () => worktree,
        ensureOnDisk: async () => { session.ensureCalls++; writeTranscript(); },
      },
      ui: { notify: (message, type) => notices.push({ message, type }) },
      isIdle: () => !activity.streaming, hasPendingMessages: () => activity.pending,
      getAsyncJobSnapshot: () => activity.snapshot, abort: () => { activity.streaming = false; },
      setInterval: callback => { intervals.push(callback); return intervals.length; },
      setTimeout: callback => { timeouts.push(callback); return timeouts.length; },
    };
    const pi = {
      on: (name, handler) => hooks.set(name, handler),
      registerCommand: (name, command) => commands.set(name, command.handler),
      sendUserMessage: message => messages.push(message),
      pi: { FileSessionStorage: class {
        claimSession(id, file) {
          leases.claims.push({ id, file });
          if (options.leased || leases.held) return undefined;
          leases.held = true;
          return () => { leases.held = false; leases.releases++; };
        }
      } },
    };
    sessionExtension(pi);
    return {
      ctx, session, activity, messages, notices,
      emit: (event, payload = {}) => hooks.get(event)?.(payload, ctx),
      command: (name, args = "") => commands.get(name)(args, ctx),
      flushTasks: async () => { while (timeouts.length) await timeouts.shift()(); },
      tick: async () => { for (const callback of intervals) await callback(); },
    };
  }
  const primary = client();
  return {
    ...primary, config, identity, transcript, prompt, requests, controller, leases, client, exit,
    start: () => primary.emit("session_start"),
    state: () => readJSON(`${configPath}.binding.json`),
    ack: () => readJSON(config.ack),
    controllerState: () => readJSON(controllerPath),
    control: (command, requestId = "control-release-1") => writeFileSync(`${configPath}.control.json`, JSON.stringify({ fence: config.fence, bindingId: config.bindingId, requestId, command }), { mode: 0o600 }),
  };
}

function readJSONLines(path) { return readFileSync(path, "utf8").trim().split("\n").map(JSON.parse); }
function assertWritable(h) {
  assert.equal(h.emit("tool_call", { toolName: "bash" }), undefined);
  assert.equal(h.state().frozen, false);
  assert.equal(h.state().released, false);
}
function assertRetained(run) {
  assert.notEqual(run.coverage.state, "integrated");
  assert.equal(run.closure.state, "not_requested");
  assert.equal(run.cleanup.state, "held");
}
const isolated = { concurrency: false, timeout: 10000 };

test("owner context suppresses stale commands and handoff proof outside awaiting_manual and accepts older records", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const run = h.controller.run;
  for (const state of ["held", "observed_closed", "not_requested", "awaiting_manual"]) {
    run.closure = {
      state, evidenceIds: [], observedAt: null, reason: state,
      manualCommand: "STALE-CLOSURE-COMMAND",
      ...(state === "awaiting_manual" ? {} : { handoff: { handoffId: "STALE-HANDOFF-PROOF" } }),
    };
    run.issueObservation = { status: state === "observed_closed" ? "closed" : "in_progress", assignee: "owner", observedAt: run.updatedAt };
    await h.command("bead-context");
    const notice = h.notices.at(-1);
    assert.equal(notice.type, "info", "optional handoff absence supports existing records");
    assert.ok(notice.message.includes(`Closure state: ${state}`));
    assert.ok(notice.message.includes(`Observed Beads status: ${run.issueObservation.status}`));
    assert.ok(!notice.message.includes("STALE-CLOSURE-COMMAND"));
    assert.ok(!notice.message.includes("STALE-HANDOFF-PROOF"));
  }
  assertWritable(h);
});

test("startup publishes its acknowledgement and task only after durable identity and controller acknowledgement", isolated, async t => {
  const h = await fixture(t);
  const reached = deferred();
  const reply = deferred();
  h.controller.beforeResponse = async (request, response) => {
    if (request.method === "observe" && request.params.observation.event.kind === "session_bound") {
      reached.resolve();
      await reply.promise;
    }
    return response;
  };
  t.after(() => reply.resolve());
  const starting = h.start();
  await reached.promise;
  assert.equal(h.session.ensureCalls, 1);
  assert.deepEqual(h.state().omp, h.identity);
  assert.deepEqual(h.state().binding, { harness: "omp", sessionId: h.identity.session_id, bindingId: h.config.bindingId });
  assert.deepEqual(h.controllerState().execution.session, h.state().binding);
  assert.equal(h.state().acknowledged, false);
  assert.equal(h.emit("tool_call").block, true, "a durable but unacknowledged association cannot run tools");
  assert.equal(existsSync(h.config.ack), false);
  await h.flushTasks();
  assert.deepEqual(h.messages, []);
  assert.equal(existsSync(h.prompt), true);
  reply.resolve();
  await starting;
  assert.deepEqual(h.ack().session, h.identity);
  assert.equal(h.state().acknowledged, true);
  assert.deepEqual(h.ack().observation, h.state().observation);
  assert.equal(h.ack().pid, process.pid);
  assert.equal(h.leases.held, true);
  assert.deepEqual(h.messages, []);
  await h.flushTasks();
  assert.deepEqual(h.messages, ["Implement the assigned Bead, without closing it.\n"]);
  assertWritable(h);
});

test("failed or foreign controller acknowledgements never dispatch the startup task", isolated, async t => {
  for (const failure of ["refused", "foreign-fence", "foreign-session", "foreign-scope", "foreign-request"]) {
    await t.test(failure, async t => {
      const h = await fixture(t);
      h.controller.beforeResponse = async (_request, response) => {
        if (failure === "refused") return { apiVersion: 1, requestId: response.requestId, outcome: "refused", error: { code: "session_mismatch", message: "not acknowledged", retryable: false, capability: null }, run: response.run, operation: null };
        if (failure === "foreign-fence") response.run.fence.generation++;
        if (failure === "foreign-session") response.run.execution.session.sessionId = "another-session";
        if (failure === "foreign-scope") response.run.scope.issueId = "another-bead";
        if (failure === "foreign-request") response.requestId = "another-request";
        return response;
      };
      await assert.rejects(h.start(), error => error instanceof ExitRequested && error.code === 1);
      await h.flushTasks();
      assert.deepEqual(h.messages, []);
      assert.equal(existsSync(h.prompt), true);
      assert.equal(h.ack().session, undefined);
      assert.equal(typeof h.ack().error, "string");
      assert.equal(h.emit("tool_call").block, true);
      assert.equal(h.leases.held, false);
    });
  }
});

test("exact resumed identity survives controller object-key reordering", isolated, async t => {
  const h = await fixture(t, { previous: {} });
  h.controller.beforeResponse = async (_request, response) => {
    response.run.execution.session = Object.fromEntries(Object.entries(response.run.execution.session).reverse());
    return response;
  };
  await h.start();
  await h.emit("before_agent_start");
  assert.deepEqual(h.ack().session, h.identity);
  assertWritable(h);
});


test("mismatched, previously associated, and leased transcripts cannot acquire ownership", isolated, async t => {
  for (const options of [
    { header: { id: "foreign-session" } },
    { header: { cwd: "/tmp" } },
    { previous: { session_id: "saved-other-session" } },
    { previous: { session_file: "/tmp/saved-other-transcript.jsonl" } },
    { previous: { machine: "saved-other-machine" } },
    { previous: { worktree: "/tmp" } },
    { leased: true },
  ]) {
    await t.test(JSON.stringify(options), async t => {
      const h = await fixture(t, options);
      await assert.rejects(h.start(), error => error instanceof ExitRequested && error.code === 1);
      await h.flushTasks();
      assert.deepEqual(h.requests, []);
      assert.deepEqual(h.messages, []);
      assert.equal(h.ack().session, undefined);
      assert.equal(h.emit("tool_call").block, true);
      assert.equal(h.controllerState().execution.session, null);
      assert.deepEqual(readJSON(process.env.GO_BD_SESSION_CONFIG).previous, h.config.previous);
      assert.equal(h.leases.held, false);
    });
  }
});

test("subagent factories cannot persist or replace the primary association or run owner commands", isolated, async t => {
  const h = await fixture(t);
  const child = h.client("sub", "child-session", join(h.config.worktree, "child.jsonl"));
  await child.emit("session_start");
  assert.equal(child.session.ensureCalls, 0);
  assert.equal(h.leases.claims.length, 0);
  assert.deepEqual(h.requests, []);
  assert.equal(child.emit("tool_call").block, true);
  await h.start();
  const saved = h.state();
  const ack = h.ack();
  const requestCount = h.requests.length;
  for (const event of ["session_start", "session_switch", "session_branch", "session_tree", "before_agent_start", "agent_end", "session_shutdown"]) await child.emit(event);
  await child.command("bead-handoff", "{}");
  await child.command("bead-release", "child must not release primary");
  await child.tick();
  assert.deepEqual(h.state(), saved);
  assert.deepEqual(h.ack(), ack);
  assert.equal(h.requests.length, requestCount);
  assert.equal(h.leases.claims.length, 1);
  assert.equal(h.leases.held, true);
  assert.equal(child.session.ensureCalls, 0);
  assert.equal(child.emit("tool_call"), undefined);
  assert.deepEqual(child.messages, []);
});

test("switch, branch, and tree are cancelled before the pinned primary association changes", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const saved = h.state();
  for (const event of ["session_before_switch", "session_before_branch", "session_before_tree"]) {
    assert.deepEqual(await h.emit(event, { reason: "switch", sessionFile: "/tmp/other.jsonl" }), { cancel: true });
    assert.deepEqual(h.state(), saved);
  }
  h.session.id = "forced-fork";
  await assert.rejects(h.emit("session_branch"), error => error instanceof ExitRequested);
  assert.deepEqual(h.state().omp, h.identity);
  assert.equal(h.state().binding.sessionId, h.identity.session_id);
  assert.equal(h.emit("tool_call").block, true);
  assert.equal(h.leases.claims.length, 1);
});

test("invalid handoff shapes cannot mutate controller or ownership state", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const saved = h.state();
  const run = h.controllerState();
  const requestCount = h.requests.length;
  for (const args of ["not JSON", "null", "[]", "{}", '{"expected":{"revision":1}}']) {
    await h.command("bead-handoff", args);
    assert.equal(h.requests.length, requestCount);
    assert.deepEqual(h.state(), saved);
    assert.deepEqual(h.controllerState(), run);
    assert.equal(h.notices.at(-1).type, "error");
    assertWritable(h);
  }
});

const busyCases = [
  ["streaming", activity => { activity.streaming = true; }],
  ["pending messages", activity => { activity.pending = true; }],
  ["running async job", activity => { activity.snapshot.running = [{ id: "job-1" }]; }],
  ["queued async delivery", activity => { activity.snapshot.delivery.queued = 1; }],
  ["delivering async result", activity => { activity.snapshot.delivery.delivering = true; }],
  ["pending async result IDs", activity => { activity.snapshot.delivery.pendingJobIds = ["job-1"]; }],
  ["unknown async snapshot", activity => { activity.snapshot = null; }],
];
function resetActivity(h) { h.activity.streaming = false; h.activity.pending = false; h.activity.snapshot = quietJobs(); }

test("explicit release refuses streaming, pending messages, and every active or unknown async delivery state", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const requestCount = h.requests.length;
  for (const [name, makeBusy] of busyCases) {
    resetActivity(h);
    makeBusy(h.activity);
    await h.command("bead-release", `release while ${name}`);
    assert.equal(h.requests.length, requestCount, name);
    assertWritable(h);
    assert.equal(h.controllerState().release.state, "held", name);
    assert.equal(h.notices.at(-1).type, "error", name);
  }
});

test("release freezes the durable primary and subagent tool gates before controller response", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const child = h.client("sub", "child-session");
  const reached = deferred();
  const reply = deferred();
  h.controller.beforeResponse = async (request, response) => {
    if (request.method === "release") { reached.resolve(); await reply.promise; }
    return response;
  };
  t.after(() => reply.resolve());
  // Completed history alone is not active owned work.
  h.activity.snapshot.recent = [{ id: "completed-job" }];
  const releasing = h.command("bead-release", "owner explicitly yields");
  await reached.promise;
  assert.equal(h.state().frozen, true);
  assert.equal(h.state().released, false);
  assert.equal(h.emit("tool_call").block, true);
  assert.equal(child.emit("tool_call").block, true);
  assert.equal(h.leases.held, true);
  assert.deepEqual(h.state().omp, h.identity);
  assertRetained(h.controllerState());
  await h.flushTasks();
  assert.deepEqual(h.messages, [], "an already queued startup task cannot cross the frozen gate");
  reply.resolve();
  await releasing;
  assert.equal(h.controllerState().release.state, "requested");
  assert.equal(h.state().released, false, "requesting release is not acknowledging release");
  assert.equal(h.leases.held, true);
  const head = "a".repeat(40);
  const target = clone(h.controllerState().target);
  const params = {
    previousCoverageRevision: 0, target, workspaceHead: head,
    requiredSubmissions: [{ submissionId: "source", submissionRevision: 1, head, target, pullRequest: null }],
    summary: "ready", nextSteps: [], risks: [],
  };
  const count = h.requests.length;
  await h.command("bead-handoff", JSON.stringify(params));
  assert.equal(h.requests.length, count, "a frozen owner cannot hand off even a complete valid submission");
  assert.equal(h.notices.at(-1).type, "error");
});

test("release rechecks owned work after awaiting current controller ownership", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const reached = deferred();
  const reply = deferred();
  h.controller.beforeResponse = async (request, response) => {
    if (request.method === "status") { reached.resolve(); await reply.promise; }
    return response;
  };
  t.after(() => reply.resolve());
  const releasing = h.command("bead-release", "owner yields if still idle");
  await reached.promise;
  h.activity.snapshot.delivery.pendingJobIds = ["just-finished-job"];
  reply.resolve();
  await releasing;
  assert.equal(h.requests.some(request => request.method === "release"), false);
  assert.equal(h.controllerState().release.state, "held");
  assertWritable(h);
  assert.equal(h.notices.at(-1).type, "error");
});

test("controller release waits for all owned work, then synchronously freezes before owner validation", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  h.controller.run.release.state = "requested";
  h.control("release");
  const requestCount = h.requests.length;
  for (const [name, makeBusy] of busyCases) {
    resetActivity(h);
    makeBusy(h.activity);
    await h.tick();
    assert.equal(h.requests.length, requestCount, name);
    assertWritable(h);
  }
  resetActivity(h);
  const child = h.client("sub", "child-session");
  const reached = deferred();
  const reply = deferred();
  h.controller.beforeResponse = async (request, response) => {
    if (request.method === "status") { reached.resolve(); await reply.promise; }
    return response;
  };
  t.after(() => reply.resolve());
  const acknowledging = h.tick();
  await reached.promise;
  assert.equal(h.state().frozen, true);
  assert.equal(h.emit("tool_call").block, true);
  assert.equal(child.emit("tool_call").block, true);
  reply.resolve();
  await acknowledging;
  assert.equal(h.state().released, true);
  assert.equal(h.controllerState().release.state, "released");
  assert.deepEqual(h.controllerState().release.receipt.session, h.state().binding);
  assertRetained(h.controllerState());
  const completedCount = h.requests.length;
  await h.tick();
  assert.equal(h.requests.length, completedCount, "release acknowledgement is not repeated");
});

test("idle turns and shutdown retain the association without release, closure, cleanup, or integration", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  await h.emit("before_agent_start");
  await h.emit("agent_end");
  for (let index = 0; index < 3; index++) await h.tick();
  assert.equal(h.controllerState().execution.state, "idle");
  assert.equal(h.controllerState().execution.activity, "active");
  assert.equal(h.controllerState().release.state, "held");
  assertWritable(h);
  assert.equal(h.leases.held, true);
  await h.emit("session_shutdown");
  const run = h.controllerState();
  assert.equal(run.execution.state, "stopped");
  assert.equal(run.execution.activity, "unknown");
  assert.equal(run.release.state, "held");
  assertRetained(run);
  assert.equal(h.state().released, false);
  assert.deepEqual(h.state().omp, h.identity);
  assert.deepEqual(h.ack().session, h.identity);
  assert.equal(existsSync(h.transcript), true);
  assert.equal(existsSync(h.config.worktree), true);
  assert.equal(h.leases.held, false);
  assert.equal(h.leases.releases, 1);
  assert.equal(h.requests.some(request => request.method === "release" || request.method === "confirm_integration"), false);
  assert.equal(h.requests.some(request => request.method === "observe" && request.params.observation.event.kind === "release_acknowledged"), false);
});

test("shutdown acknowledgement failure is best effort and retains durable ownership evidence", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  h.controller.beforeResponse = async (_request, response) => ({
    apiVersion: 1, requestId: response.requestId, outcome: "refused",
    error: { code: "session_mismatch", message: "shutdown not acknowledged", retryable: false, capability: null },
    run: response.run, operation: null,
  });
  await h.emit("session_shutdown");
  assert.equal(h.exit.mock.calls.length, 0);
  assert.equal(h.leases.held, false);
  assert.deepEqual(h.state().omp, h.identity);
  assert.deepEqual(h.ack().session, h.identity);
  assert.equal(h.state().released, false);
  assert.equal(h.controllerState().release.state, "held");
  assertRetained(h.controllerState());
  assert.equal(existsSync(h.transcript), true);
  assert.equal(existsSync(h.config.worktree), true);
});

test("read-only enforcement drains owned work before acknowledgement and still permits explicit idle release", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  h.control("read_only", "read-only-1");
  h.activity.streaming = true;
  h.activity.snapshot.running = [{ id: "owned-process" }];
  await h.tick();
  assert.equal(h.state().frozen, true);
  assert.equal(h.emit("tool_call").block, true);
  assert.equal(h.client("sub").emit("tool_call").block, true);
  assert.equal(h.controllerState().execution.readOnlyAcknowledged, false);
  assert.equal(h.controllerState().release.state, "held");
  resetActivity(h);
  await h.tick();
  assert.equal(h.controllerState().execution.readOnlyAcknowledged, true);
  assert.equal(h.state().released, false);
  await h.command("bead-release", "owner relinquishes read-only session");
  assert.equal(h.controllerState().release.state, "requested");
  h.control("release", "release-read-only-1");
  await h.tick();
  assert.equal(h.controllerState().release.state, "released");
  assertRetained(h.controllerState());
});

test("shutdown never waits behind a stalled owner request and fences tools without asserting release", isolated, async t => {
  const h = await fixture(t);
  await h.start();
  const reached = deferred();
  const reply = deferred();
  h.controller.beforeResponse = async (request, response) => {
    if (request.method === "status") { reached.resolve(); await reply.promise; }
    return response;
  };
  t.after(() => reply.resolve());
  const context = h.command("bead-context");
  await reached.promise;
  await h.emit("session_shutdown");
  assert.equal(h.leases.held, false);
  assert.equal(h.state().frozen, true);
  assert.equal(h.emit("tool_call").block, true);
  assert.equal(h.state().released, false);
  assert.equal(h.requests.some(request => request.params.observation?.event.kind === "exited"), false);
  assert.equal(h.controllerState().release.state, "held");
  reply.resolve();
  await context;
});

test("slow durable controller acknowledgement remains gated until accepted, beyond the old four-second deadline", { ...isolated, timeout: 15000 }, async t => {
  const h = await fixture(t);
  h.controller.beforeResponse = async (_request, response) => {
    assert.equal(existsSync(h.config.ack), false);
    assert.equal(h.state().acknowledged, false);
    await new Promise(resolve => setTimeout(resolve, 4500));
    assert.deepEqual(h.messages, []);
    return response;
  };
  await h.start();
  assert.equal(h.state().acknowledged, true);
  assert.deepEqual(h.ack().session, h.identity);
  await h.flushTasks();
  assert.deepEqual(h.messages, ["Implement the assigned Bead, without closing it.\n"]);
});

test("controller calls share one bounded action budget and cannot run work after it expires", isolated, async t => {
  let clock = 0;
  t.mock.method(performance, "now", () => clock);
  const h = await fixture(t);
  await h.start();
  h.controller.beforeResponse = async (request, response) => {
    if (request.method === "status") clock = 26000;
    return response;
  };
  await assert.rejects(h.emit("before_agent_start"), error => error instanceof ExitRequested);
  assert.equal(h.requests.some(request => request.params.observation?.event.kind === "activity"), false);
  assert.equal(h.emit("tool_call").block, true);
  assert.equal(h.state().frozen, true);
});
