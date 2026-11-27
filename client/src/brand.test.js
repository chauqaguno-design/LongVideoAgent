import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("current application uses the CutSieve product name", () => {
  for (const path of [
    "../index.html",
    "./App.vue",
    "./editing/WorkflowTurn.vue",
  ]) {
    const source = readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(source, /CutSieve/);
    assert.doesNotMatch(source, /DOVideo|DOVIDEO|ReelCue/);
  }
});
