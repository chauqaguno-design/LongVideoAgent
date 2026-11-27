import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("application uses the CutSieve brand without legacy traces", () => {
  for (const path of ["../index.html", "./App.vue"]) {
    const source = readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(source, /CutSieve/);
    assert.doesNotMatch(source, /DOVideo|DOVIDEO|ReelCue|VideoMind|Pixel Future/);
  }
});
