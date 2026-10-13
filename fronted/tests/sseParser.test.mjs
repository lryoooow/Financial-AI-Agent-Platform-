import test from "node:test";
import assert from "node:assert/strict";
import { createSseEventGate, parseSseFrames } from "../src/app/api/sseParser.js";

test("解析 CRLF 与多个 SSE 帧", () => {
  const parsed = parseSseFrames('data: {"type":"token","text":"中"}\r\n\r\ndata: {"type":"done"}\r\n\r\n');
  assert.deepEqual(parsed.events, [{ type: "token", text: "中" }, { type: "done" }]);
  assert.equal(parsed.rest, "");
});

test("保留跨 chunk 残帧并在下一块完成", () => {
  const first = parseSseFrames('data: {"type":"token","text":"A"}\n\ndata: {"type":"token"');
  assert.deepEqual(first.events, [{ type: "token", text: "A" }]);
  const second = parseSseFrames(first.rest + ',"text":"B"}\n\n');
  assert.deepEqual(second.events, [{ type: "token", text: "B" }]);
});

test("flush 解析没有终止空行的尾帧", () => {
  const parsed = parseSseFrames('data: {"type":"error","message":"断流"}', true);
  assert.deepEqual(parsed.events, [{ type: "error", message: "断流" }]);
  assert.equal(parsed.rest, "");
});

test("只接受一个终态并忽略终态后的帧", () => {
  const gate = createSseEventGate();
  assert.equal(gate.accept({ type: "token", text: "A" }), true);
  assert.equal(gate.accept({ type: "done" }), true);
  assert.equal(gate.accept({ type: "error", message: "重复终态" }), false);
  assert.equal(gate.accept({ type: "token", text: "B" }), false);
  assert.equal(gate.isTerminal(), true);
});
