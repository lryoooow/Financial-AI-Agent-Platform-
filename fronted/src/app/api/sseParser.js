/**
 * 按 SSE 空行边界解析 JSON data 帧，兼容 CRLF、跨 chunk 残帧与流末尾尾帧。
 * @param {string} buffer
 * @param {boolean} flush
 * @returns {{events: any[], rest: string}}
 */
export function parseSseFrames(buffer, flush = false) {
  const normalized = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const frames = normalized.split("\n\n");
  const rest = flush ? "" : (frames.pop() || "");
  const events = [];
  for (const frame of frames) {
    const payload = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n")
      .trim();
    if (!payload) continue;
    try { events.push(JSON.parse(payload)); } catch {}
  }
  return { events, rest };
}

/** 确保一个 SSE 响应只处理一个终态，终态后的帧全部忽略。 */
export function createSseEventGate() {
  let terminal = false;
  return {
    accept(event) {
      if (terminal) return false;
      if (event?.type === "done" || event?.type === "error") terminal = true;
      return true;
    },
    isTerminal() {
      return terminal;
    },
  };
}
