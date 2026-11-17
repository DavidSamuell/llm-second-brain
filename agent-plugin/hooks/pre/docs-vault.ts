// Docs vault session hooks for omp (SPEC-session-hooks). omp loads plugin `hooks/pre/*.ts` as
// extension factories; it ignores the Claude Code `hooks/hooks.json`, so nothing runs twice.
//
// On `before_agent_start` (every prompt) it runs the plugin's own shell scripts, the same ones
// Claude Code runs: `prompt-nudge.sh` on every prompt (opt-outs, allowlist, a reminder every 10th
// prompt) and `session-start.sh` until its context block has been injected in this session, and
// again after `session_clear` or `session_compact`. Scripts get JSON on stdin, fixed argv and a
// minimal environment; they read no credentials and print nothing when capture is off.
// Any failure or timeout means no message: a hook never breaks a prompt.
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HOOKS_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const TIMEOUT_MS = 5000;
const MAX_CONTEXT = 1500;
const ENV_KEYS = ["PATH", "HOME", "TMPDIR", "DOCS_VAULT_CAPTURE"];

type HookContext = { cwd?: string; sessionManager?: { getSessionId?: () => string } };
type ContextMessage = {
	customType?: string;
	content?: string | Array<{ type?: string; text?: string }>;
};
type Handler = (
	event: { reason?: string; messages?: ContextMessage[] } | undefined,
	ctx: HookContext | undefined,
) => unknown;
type ExtensionApi = { on(event: string, handler: Handler): void };

function scriptEnv(): Record<string, string> {
	const env: Record<string, string> = {};
	for (const key of ENV_KEYS) {
		const value = process.env[key];
		if (value !== undefined) env[key] = value;
	}
	return env;
}

// Runs one hook script and returns its additionalContext, or undefined.
function runScript(script: string, input: Record<string, string>): Promise<string | undefined> {
	const { promise, resolve } = Promise.withResolvers<string | undefined>();
	try {
		const child = execFile(
			"bash",
			[join(HOOKS_DIR, script)],
			{ env: scriptEnv(), timeout: TIMEOUT_MS, maxBuffer: 64 * 1024, windowsHide: true },
			(error, stdout) => {
				if (error) return resolve(undefined);
				try {
					const context = JSON.parse(String(stdout))?.hookSpecificOutput?.additionalContext;
					const ok = typeof context === "string" && context.length > 0 && context.length <= MAX_CONTEXT;
					resolve(ok ? context : undefined);
				} catch {
					resolve(undefined);
				}
			},
		);
		child.stdin?.on("error", () => {});
		child.stdin?.end(JSON.stringify(input));
	} catch {
		resolve(undefined);
	}
	return promise;
}

export default function docsVault(pi: ExtensionApi): void {
	const fallbackId = randomUUID();
	const injected = new Set<string>();
	const reinjectReason = new Map<string, string>();
	const pending = new Map<string, string>();

	const sessionId = (ctx: HookContext | undefined): string => {
		try {
			const id = ctx?.sessionManager?.getSessionId?.();
			if (typeof id === "string" && id.length > 0) return id;
		} catch {}
		return fallbackId;
	};
	const reinject = (reason: string): Handler => (_event, ctx) => {
		const id = sessionId(ctx);
		injected.delete(id);
		pending.delete(id);
		reinjectReason.set(id, reason);
	};

	pi.on("session_clear", reinject("clear"));
	pi.on("session_compact", reinject("compact"));

	// Only published context counts as delivered. before_agent_start can be replayed after
	// the SDK discards an attempt (omp 18.22.0, extensions.md: Prompt and turn lifecycle).
	pi.on("context", (event, ctx) => {
		const id = sessionId(ctx);
		const candidate = pending.get(id);
		if (candidate === undefined || !Array.isArray(event?.messages)) return;
		const delivered = event.messages.findLast(message => {
			if (message.customType !== "docs-vault-capture") return false;
			const content = message.content;
			return content === candidate || (
				Array.isArray(content) && content.length === 1 &&
				content[0]?.type === "text" && content[0]?.text === candidate
			);
		});
		if (delivered) {
			injected.add(id);
			pending.delete(id);
			reinjectReason.delete(id);
		}
	});

	pi.on("before_agent_start", async (event, ctx) => {
		try {
			// A revived subagent replays this event with an empty prompt and discards the result.
			if (event?.reason === "revive") return undefined;
			const id = sessionId(ctx);
			pending.delete(id);
			const cwd = ctx?.cwd ?? process.cwd();
			let content = await runScript("prompt-nudge.sh", { session_id: id, cwd });
			if (!injected.has(id)) {
				const source = reinjectReason.get(id) ?? "startup";
				const block = await runScript("session-start.sh", { cwd, source });
				if (block !== undefined) {
					pending.set(id, block);
					content = block;
				}
			}
			if (content === undefined) return undefined;
			return { message: { customType: "docs-vault-capture", content, display: false } };
		} catch {
			return undefined;
		}
	});
}
