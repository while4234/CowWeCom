#!/usr/bin/env node
import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULT_TIMEOUT_MS = 45000;

function envPath(name, fallback) {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : fallback;
}

function homePath(...parts) {
  return path.join(os.homedir(), ...parts);
}

function firstExistingPath(paths) {
  return paths.find((candidate) => existsSync(candidate)) ?? paths[0];
}

function resolveOpenClawRoot() {
  return envPath("OPENCLAW_QQ_ROOT", envPath("OPENCLAW_ROOT", homePath(".openclaw")));
}

function resolveCodexDist() {
  return envPath(
    "OPENCLAW_CODEX_DIST",
    firstExistingPath([
      path.join(resolveOpenClawRoot(), "extensions", "codex", "dist"),
      homePath(".openclaw", "extensions", "codex", "dist"),
      homePath(".openclaw-qq", "extensions", "codex", "dist"),
    ]),
  );
}

async function findDistModule(prefix, fallback) {
  const dist = resolveCodexDist();
  const fallbackPath = path.join(dist, fallback);
  if (existsSync(fallbackPath)) return fallbackPath;
  const entries = await readdir(dist);
  const found = entries.find((name) => name.startsWith(prefix) && name.endsWith(".js"));
  if (!found) throw new Error(`Codex extension module not found: ${prefix}*.js in ${dist}`);
  return path.join(dist, found);
}

async function importCodexDistModule(prefix, fallback) {
  const modulePath = await findDistModule(prefix, fallback);
  return import(pathToFileURL(modulePath).href);
}

async function readJsonFile(filePath, fallback) {
  if (!filePath || !existsSync(filePath)) return fallback;
  const raw = await readFile(filePath, "utf8");
  return JSON.parse(raw.replace(/^\uFEFF/, ""));
}

function resolveConfigPath() {
  return envPath(
    "OPENCLAW_QQ_CONFIG",
    envPath(
      "OPENCLAW_CONFIG",
      firstExistingPath([
        path.join(resolveOpenClawRoot(), "openclaw.json"),
        homePath(".openclaw-qq", "openclaw.json"),
      ]),
    ),
  );
}

function resolveAgentDir() {
  return envPath(
    "OPENCLAW_CODEX_AGENT_DIR",
    firstExistingPath([
      path.join(resolveOpenClawRoot(), "agents", "main", "agent"),
      homePath(".openclaw", "agents", "main", "agent"),
      homePath(".openclaw-qq", "agents", "qq_openclaw", "agent"),
    ]),
  );
}

function asRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function numberOrNull(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function maskEmail(value) {
  const text = String(value || "unknown");
  const at = text.indexOf("@");
  if (at <= 1) return text;
  return `${text.slice(0, 1)}***${text.slice(at)}`;
}

function readAccount(accountResponse, options = {}) {
  const outer = asRecord(accountResponse);
  const account = asRecord(outer.account);
  const source = Object.keys(account).length > 0 ? account : outer;
  const rawEmail = String(source.email ?? source.accountEmail ?? "unknown");
  const masked = options.showAccount ? rawEmail : maskEmail(rawEmail);
  return {
    email: masked,
    label: masked,
    email_masked: masked,
    planType: String(source.planType ?? source.plan_type ?? "unknown"),
    plan_type: String(source.planType ?? source.plan_type ?? "unknown"),
    type: String(source.type ?? "unknown"),
    requiresOpenaiAuth: outer.requiresOpenaiAuth === true,
    requires_openai_auth: outer.requiresOpenaiAuth === true,
  };
}

function collectSnapshots(rateLimitResponse) {
  const root = asRecord(rateLimitResponse);
  const byLimitId = asRecord(root.rateLimitsByLimitId ?? root.rate_limits_by_limit_id);
  const snapshots = Object.values(byLimitId).filter((entry) => Object.keys(asRecord(entry)).length > 0);
  if (snapshots.length > 0) return snapshots.sort(compareSnapshots);
  const single = asRecord(root.rateLimits ?? root.rate_limits);
  return Object.keys(single).length > 0 ? [single] : [];
}

function compareSnapshots(left, right) {
  const leftId = String(asRecord(left).limitId ?? asRecord(left).limit_id ?? "");
  const rightId = String(asRecord(right).limitId ?? asRecord(right).limit_id ?? "");
  if (leftId === "codex") return -1;
  if (rightId === "codex") return 1;
  return labelForSnapshot(left).localeCompare(labelForSnapshot(right));
}

function labelForSnapshot(snapshot) {
  const record = asRecord(snapshot);
  return String(record.limitName ?? record.limit_name ?? record.limitId ?? record.limit_id ?? "Codex");
}

function percentText(value) {
  const percent = numberOrNull(value);
  return percent === null ? "unknown" : `${percent}%`;
}

function remainingText(value) {
  const percent = numberOrNull(value);
  if (percent === null) return "unknown";
  return `about ${Math.max(0, Math.round((100 - percent) * 10) / 10)}%`;
}

function windowText(minutes) {
  const mins = numberOrNull(minutes);
  if (mins === null) return "window";
  if (mins % 10080 === 0) return `${(mins / 10080) * 7} day window`;
  if (mins % 1440 === 0) return `${mins / 1440} day window`;
  if (mins % 60 === 0) return `${mins / 60} hour window`;
  return `${mins} minute window`;
}

function resetText(value) {
  const numeric = numberOrNull(value);
  let millis = null;
  if (numeric !== null) {
    millis = numeric > 1e12 ? numeric : numeric * 1000;
  } else if (typeof value === "string" && value.trim()) {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) millis = parsed;
  }
  if (millis === null) return "unknown";
  const formatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return `${formatter.format(new Date(millis)).replaceAll("/", "-")} UTC+8`;
}

function normalizeWindow(name, value) {
  const record = asRecord(value);
  if (Object.keys(record).length === 0) return null;
  const usedPercent = numberOrNull(record.usedPercent ?? record.used_percent);
  const windowMinutes = numberOrNull(
    record.windowDurationMins ?? record.window_duration_mins ?? record.windowMinutes ?? record.window_minutes,
  );
  const resetsAt = numberOrNull(record.resetsAt ?? record.resets_at);
  return {
    name,
    used_percent: usedPercent,
    remaining_percent: usedPercent === null ? null : Math.max(0, Math.round((100 - usedPercent) * 10) / 10),
    window_minutes: windowMinutes,
    reset_at: resetsAt === null ? null : new Date(resetsAt > 1e12 ? resetsAt : resetsAt * 1000).toISOString(),
  };
}

function normalizeSnapshot(snapshot) {
  const record = asRecord(snapshot);
  const limitId = String(record.limitId ?? record.limit_id ?? "");
  const reachedType = record.rateLimitReachedType ?? record.rate_limit_reached_type ?? "";
  const windows = ["primary", "secondary"].flatMap((name) => {
    const window = normalizeWindow(name, record[name]);
    return window ? [window] : [];
  });
  return {
    limit_id: limitId || null,
    limit_name: limitId === "codex" ? "Codex" : labelForSnapshot(record),
    status: reachedType || windows.some((item) => item.used_percent !== null && item.used_percent >= 100) ? "blocked" : "available",
    reached_type: reachedType || null,
    windows,
  };
}

function formatPeriod(period) {
  const record = asRecord(period);
  if (Object.keys(record).length === 0) return "";
  const usedPercent = record.usedPercent ?? record.used_percent;
  const windowMinutes = record.windowDurationMins ?? record.window_duration_mins ?? record.windowMinutes ?? record.window_minutes;
  const resetsAt = record.resetsAt ?? record.resets_at;
  return `- ${windowText(windowMinutes)}: used ${percentText(usedPercent)}, remaining ${remainingText(usedPercent)}, resets ${resetText(resetsAt)}`;
}

function formatCredits(snapshot) {
  const credits = asRecord(asRecord(snapshot).credits);
  if (Object.keys(credits).length === 0) return "";
  const balance = String(credits.balance ?? "0");
  if (credits.hasCredits === false && credits.unlimited === false) {
    return `- extra credits: ${balance} (no extra balance; subscription quota may still be available)`;
  }
  if (credits.unlimited === true) return "- extra credits: unlimited";
  return `- extra credits: ${balance}`;
}

function formatSnapshot(snapshot) {
  const record = asRecord(snapshot);
  const reached = record.rateLimitReachedType ?? record.rate_limit_reached_type;
  const lines = [
    `${labelForSnapshot(record)}: ${reached ? `blocked (${reached})` : "available"}`,
    formatPeriod(record.primary),
    formatPeriod(record.secondary),
    formatCredits(record),
  ].filter(Boolean);
  return lines.join("\n");
}

function combinedPayload(accountResponse, rateLimitResponse, options = {}) {
  const rateRoot = asRecord(rateLimitResponse);
  const snapshots = collectSnapshots(rateRoot);
  const rateLimits = snapshots.map(normalizeSnapshot);
  const byLimitId = asRecord(rateRoot.rateLimitsByLimitId ?? rateRoot.rate_limits_by_limit_id);
  const single = asRecord(rateRoot.rateLimits ?? rateRoot.rate_limits);
  return {
    ok: true,
    source: "openclaw-codex-app-server",
    account: readAccount(accountResponse, options),
    summary: {
      blocked: rateLimits.some((item) => item.status === "blocked"),
    },
    rate_limits: rateLimits,
    rateLimitsByLimitId: byLimitId,
    rate_limits_by_limit_id: byLimitId,
    rateLimits: single,
    rate_limits_raw: single,
    snapshots,
  };
}

function formatQuota(payload) {
  const account = asRecord(payload.account);
  const rateLimits = Array.isArray(payload.rate_limits) ? payload.rate_limits : [];
  const lines = [
    "GPT/Codex quota",
    `- account: ${account.label ?? account.email_masked ?? "unknown"}`,
    `- plan: ${account.plan_type ?? "unknown"}`,
    `- account type: ${account.type ?? "unknown"}`,
    `- status: ${asRecord(payload.summary).blocked ? "blocked" : "available"}`,
    "",
  ];
  if (rateLimits.length === 0) {
    lines.push("- no Codex rate limit data returned");
  } else {
    for (const limit of rateLimits) {
      lines.push(`${limit.limit_name}: ${limit.status}${limit.reached_type ? ` (${limit.reached_type})` : ""}`);
      for (const window of limit.windows) {
        lines.push(`- ${windowText(window.window_minutes)}: used ${percentText(window.used_percent)}, remaining ${remainingText(window.used_percent)}, resets ${resetText(window.reset_at)}`);
      }
      lines.push("");
    }
  }
  return lines.join("\n").trim();
}

function argValue(name, fallback = "") {
  const idx = process.argv.indexOf(name);
  if (idx >= 0 && process.argv[idx + 1]) return process.argv[idx + 1];
  return fallback;
}

function parseArgs() {
  return {
    format: argValue("--format", process.argv.includes("--json") ? "json" : "text"),
    timeoutMs: argValue("--timeout-ms", ""),
    showAccount: process.argv.includes("--show-account"),
    mockAccountFile: argValue("--mock-account-file", ""),
    mockRateLimitsFile: argValue("--mock-rate-limits-file", ""),
  };
}

function readTimeoutMs(args) {
  const raw =
    args.timeoutMs ||
    process.env.QQ_OPENCLAW_CODEX_QUOTA_TIMEOUT_MS ||
    process.env.OPENCLAW_CODEX_QUOTA_TIMEOUT_MS ||
    DEFAULT_TIMEOUT_MS;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : DEFAULT_TIMEOUT_MS;
}

async function main() {
  const args = parseArgs();
  if (args.mockAccountFile || args.mockRateLimitsFile) {
    const account = await readJsonFile(args.mockAccountFile, {});
    const limits = await readJsonFile(args.mockRateLimitsFile, {});
    const payload = combinedPayload(account, limits, { showAccount: args.showAccount });
    console.log(args.format === "json" ? JSON.stringify(payload, null, 2) : formatQuota(payload));
    return;
  }

  const config = await readJsonFile(resolveConfigPath(), { plugins: { entries: { codex: { config: {} } } } });
  const pluginConfig = asRecord(asRecord(asRecord(config.plugins).entries).codex).config ?? {};
  const { n: methods } = await importCodexDistModule("request-", "request-ohCy5ASa.js");
  const { s: resolveRuntimeOptions } = await importCodexDistModule("config-", "config-0rd3LnKg.js");
  const { n: createIsolatedCodexAppServerClient } = await importCodexDistModule("shared-client-", "shared-client-Cr6W-a2G.js");
  const runtime = resolveRuntimeOptions({ pluginConfig });
  const timeoutMs = readTimeoutMs(args);
  const client = await createIsolatedCodexAppServerClient({
    startOptions: runtime.start,
    agentDir: resolveAgentDir(),
    config,
    timeoutMs,
  });
  try {
    const [account, limits] = await Promise.all([
      client.request(methods.account, { refreshToken: false }, { timeoutMs }),
      client.request(methods.rateLimits, undefined, { timeoutMs }),
    ]);
    const payload = combinedPayload(account, limits, { showAccount: args.showAccount });
    console.log(args.format === "json" ? JSON.stringify(payload, null, 2) : formatQuota(payload));
  } finally {
    await client.closeAndWait({
      exitTimeoutMs: 2000,
      forceKillDelayMs: 250,
    });
  }
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  if (process.argv.includes("--json") || process.argv.includes("json")) {
    console.log(JSON.stringify({ ok: false, error: message }, null, 2));
  } else {
    console.error(`Failed to query GPT/Codex quota: ${message}`);
  }
  process.exitCode = 1;
});
