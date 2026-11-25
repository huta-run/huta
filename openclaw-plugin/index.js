// huta press relay for OpenClaw (written against OpenClaw 2026.9.4).
//
// Claims Telegram callback_data in the "huta" namespace ("huta:<anything>"), forwards the press to
// huta's local unix socket and shows huta's reply. Claimed callbacks never reach the LLM agent.
//
// Notes on the OpenClaw contract (see README.md):
// - OpenClaw answers the callback query itself, with no text, before plugin handlers run, so a
//   Telegram toast (answerCallbackQuery text) cannot be set from here. The toast text is posted
//   as a short chat message instead (config feedback: "reply"), or dropped (feedback: "none").
// - The handler must never throw and must not return { handled: false }: either would let the
//   press fall through (retry or the agent). Errors are turned into the fixed fallback text.
// - callback_data is a one-time secret: it is never logged.
import net from "node:net";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const NAMESPACE = "huta";
const DEFAULT_SOCKET = "/run/huta/press.sock";
const DEFAULT_TIMEOUT_MS = 10000;
const MAX_REPLY_BYTES = 4096;
const MAX_TOAST_CHARS = 200;
const UNAVAILABLE = "huta is unavailable; use sudo huta approve on the host";

function toInt(value) {
  if (typeof value === "number" && Number.isSafeInteger(value)) return value;
  if (typeof value === "string" && /^-?\d+$/.test(value.trim())) {
    const n = Number(value.trim());
    if (Number.isSafeInteger(n)) return n;
  }
  return null;
}

// One request/reply exchange: write the line, half-close, read at most MAX_REPLY_BYTES until EOF
// (or the first newline), all within timeoutMs. Resolves with the parsed reply or rejects.
function exchange(socketPath, line, timeoutMs) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const sock = net.createConnection({ path: socketPath, allowHalfOpen: true });

    const finish = (err, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      sock.destroy();
      if (err) reject(err);
      else resolve(value);
    };
    const parse = () => {
      const text = Buffer.concat(chunks).toString("utf8");
      const first = text.split("\n", 1)[0].trim();
      if (!first) return finish(new Error("empty reply"));
      let reply;
      try {
        reply = JSON.parse(first);
      } catch {
        return finish(new Error("reply is not JSON"));
      }
      if (!reply || typeof reply !== "object" || typeof reply.ok !== "boolean" || typeof reply.toast !== "string") {
        return finish(new Error("reply has the wrong shape"));
      }
      finish(null, reply);
    };

    const timer = setTimeout(() => finish(new Error("timeout")), timeoutMs);
    sock.on("connect", () => {
      sock.end(line); // write, then half-close our side
    });
    sock.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_REPLY_BYTES) return finish(new Error("reply too large"));
      chunks.push(chunk);
      if (chunk.includes(0x0a)) parse(); // one JSON line is the whole reply
    });
    sock.on("end", parse);
    sock.on("error", (err) => finish(err));
    sock.on("close", () => finish(new Error("closed before reply")));
  });
}

function clampToast(text) {
  const clean = String(text).replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
  const chars = Array.from(clean);
  return chars.length > MAX_TOAST_CHARS ? chars.slice(0, MAX_TOAST_CHARS - 1).join("") + "…" : clean;
}

export default definePluginEntry({
  id: "huta-press",
  name: "huta press relay",
  description: "Relays Telegram inline-button presses with callback_data huta:... to huta's local unix socket",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const socketPath = typeof cfg.socketPath === "string" && cfg.socketPath ? cfg.socketPath : DEFAULT_SOCKET;
    const timeoutMs = typeof cfg.timeoutMs === "number" && cfg.timeoutMs > 0 ? cfg.timeoutMs : DEFAULT_TIMEOUT_MS;
    // errors (default): huta edits its own DM message on success, so only problems get a reply.
    const feedback = ["reply", "none"].includes(cfg.feedback) ? cfg.feedback : "errors";
    const log = api.logger;

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: NAMESPACE,
      async handler(ctx) {
        let toast = UNAVAILABLE;
        let ok = false;
        try {
          log?.info?.("huta button pressed");
          const data = ctx?.callback?.data;
          const fromId = toInt(ctx?.senderId);
          const chatId = toInt(ctx?.callback?.chatId);
          const messageId = toInt(ctx?.callback?.messageId);
          if (typeof data !== "string" || fromId === null || chatId === null || messageId === null) {
            throw new Error("press is missing a field");
          }
          const line =
            JSON.stringify({ v: 1, data, from_id: fromId, chat_id: chatId, message_id: messageId }) + "\n";
          const reply = await exchange(socketPath, line, timeoutMs);
          toast = clampToast(reply.toast) || UNAVAILABLE;
          ok = reply.ok === true;
        } catch (err) {
          // err messages here never contain callback_data
          log?.warn?.(`huta press relay failed: ${err && err.message ? err.message : "error"}`);
          toast = UNAVAILABLE;
        }
        if (feedback === "reply" || (feedback === "errors" && !ok)) {
          try {
            await ctx.respond.reply({ text: toast });
          } catch {
            log?.warn?.("huta press relay: could not post the reply");
          }
        }
        return { handled: true };
      },
    });
  },
});
