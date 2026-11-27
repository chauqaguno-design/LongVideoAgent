import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("current application uses the ReelCue product name", () => {
  for (const path of [
    "../index.html",
    "./App.vue",
  ]) {
    const source = readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(source, /ReelCue/);
    assert.doesNotMatch(source, /DOVideo|DOVIDEO/);
  }
});
