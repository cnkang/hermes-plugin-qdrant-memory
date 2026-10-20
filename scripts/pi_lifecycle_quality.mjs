/** Actual Pi hooks; assistant is deterministic, Owner extraction/embeddings are real. */
import path from "node:path";
import { randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";

const [adapterRoot, home] = process.argv.slice(2);
if (!adapterRoot || !home) throw new Error("Explicit adapter checkout and isolated home required");
const fromAdapter = file => import(pathToFileURL(path.join(adapterRoot, file)).href);
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } =
  await fromAdapter("integrations/agent-hosts/node_modules/@earendil-works/pi-coding-agent/dist/index.js");
const { createAssistantMessageEventStream } = await fromAdapter(
  "integrations/agent-hosts/node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js");
const { createOwnerMemoryExtension, ownerRequest } = await fromAdapter("integrations/pi/owner-memory.mjs");
let input = "";
for await (const chunk of process.stdin) input += chunk;
const cases = JSON.parse(input);
const captures = [], latencies = [];
let current;
const request = async (mode, value, env) => {
  const started = performance.now();
  try {
    const result = await ownerRequest(mode, value, env);
    if (mode === "remote-event") captures.push({ case_id: current, event: value, receipt: result, status: result.status });
    return result;
  } finally { latencies.push({ mode, ms: performance.now() - started }); }
};
function assistant(model) {
  const stream = createAssistantMessageEventStream();
  const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
    content: [{ type: "text", text: "ack" }], stopReason: "stop", timestamp: Date.now(),
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
  queueMicrotask(() => { stream.push({ type: "done", reason: "stop", message }); stream.end(message); });
  return stream;
}
const modelRuntime = await ModelRuntime.create({ authPath: path.join(home, "auth.json"), modelsPath: null,
  modelsStorePath: path.join(home, "models.json"), refreshOnCreate: false, allowModelNetwork: false });
// The local SDK fixture requires a nonempty key; generate it per run, never use a real credential.
modelRuntime.registerProvider("fixture", { api: "openai-completions", baseUrl: "http://127.0.0.1:1",
  apiKey: randomBytes(24).toString("hex"), streamSimple: assistant, models: [{ id: "fixture", name: "fixture",
    reasoning: false, input: ["text"], contextWindow: 32768, maxTokens: 4096,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
const settingsManager = SettingsManager.inMemory({});
const loader = new DefaultResourceLoader({ cwd: home, agentDir: home, settingsManager,
  extensionFactories: [createOwnerMemoryExtension({ env: { ...process.env,
    QDRANT_MEMORY_AUTO_CAPTURE: "1", QDRANT_MEMORY_AUTO_RECALL: "1" }, request })] });
await loader.reload();
const { session } = await createAgentSession({ cwd: home, agentDir: home, resourceLoader: loader,
  modelRuntime, model: modelRuntime.getModel("fixture", "fixture"), settingsManager,
  sessionManager: SessionManager.inMemory(home), tools: [], thinkingLevel: "off" });
try {
  for (const row of cases) {
    current = row.id;
    await session.prompt(row.user);
  }
  const historyLeak = session.agent.state.messages.some(row => row.customType === "qdrant-owner-recall");
  process.stdout.write(JSON.stringify({ captures, latencies, history_leak: historyLeak }) + "\n");
} finally { session.dispose(); }
