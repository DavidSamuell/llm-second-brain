// Test-only AGENT_COMMAND (plan D30 contract): a scripted "agent" that ingests one source
// through vault-mcp with VAULT_MCP_URL / VAULT_MCP_TOKEN and prints total_cost_usd.
const url = process.env.VAULT_MCP_URL;
const auth = { Authorization: `Bearer ${process.env.VAULT_MCP_TOKEN}` };
let session = null;
let id = 0;

async function rpc(method, params, notify = false) {
  const headers = { ...auth, "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  if (session) headers["mcp-session-id"] = session;
  const body = { jsonrpc: "2.0", method, params, ...(notify ? {} : { id: ++id }) };
  const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  session = res.headers.get("mcp-session-id") ?? session;
  if (notify) return null;
  const text = await res.text();
  const json = text.trim().startsWith("{") ? text : text.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).pop();
  const msg = JSON.parse(json);
  if (msg.error) throw new Error(JSON.stringify(msg.error));
  return msg.result;
}

async function tool(name, args) {
  const result = await rpc("tools/call", { name, arguments: args });
  if (result.isError) throw new Error(`${name}: ${result.content[0].text}`);
  return result.structuredContent ?? JSON.parse(result.content[0].text);
}

await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "stub-agent", version: "1" } });
await rpc("notifications/initialized", {}, true);
const listing = await tool("list", { folder: "raw" });
const source = listing.entries.find((e) => e.type === "file" && e.path.endsWith(".md"));
if (!source) {
  console.log(JSON.stringify({ total_cost_usd: 0.01, result: "SUMMARY: nothing to ingest" }));
  process.exit(0);
}
const claimed = await tool("claim_source", { path: source.path });
const page = await tool("read", { path: claimed.path });
const name = claimed.path.split("/").pop();
const summary = `wiki/sources/${name}`;
await tool("write", {
  path: summary,
  content: `---\ntitle: ${name}\nmaintainer: agent\ntype: source-summary\n---\n# ${name}\n\n${page.content.split("\n").length} lines summarised.\n`,
  base_version: null,
});
await tool("complete_source", { path: claimed.path, summary_page: summary });
console.log(JSON.stringify({ total_cost_usd: 0.5, result: `SUMMARY: ingested ${name}` }));
