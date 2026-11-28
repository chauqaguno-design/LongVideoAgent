import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'

const source = path => readFileSync(new URL(path, import.meta.url), 'utf8')

test('CutSieve is the only brand on the single studio entry point', () => {
  assert.match(source('../index.html'), /CutSieve · 剪筛工作台/)
  assert.match(source('./App.vue'), /CutSieve 剪筛工作台/)
  assert.match(source('./App.vue'), /<StudioWorkspace>/)

  const studioFiles = readdirSync(new URL('./editing/', import.meta.url))
  for (const name of ['EditProject.vue', 'EditStudio.vue', 'SearchPanel.vue', 'SourceMoment.vue', 'WorkflowPanel.vue', 'WorkflowTurn.vue']) {
    assert.ok(!studioFiles.includes(name), name + ' is obsolete')
  }
  for (const path of ['../index.html', './App.vue', ...studioFiles.filter(name => name.endsWith('.vue')).map(name => './editing/' + name)]) {
    assert.doesNotMatch(source(path), /DOVideo|ReelCue|VideoMind|Pixel Future|\b(?:login|register)\b|project-tabs/i, path)
  }
})

test('the darkroom palette and tabular timecode styles remain available', () => {
  const css = source('./style.css')
  for (const value of ['#090b0f', '#121620', '#171d2a', 'rgba(148, 163, 184, 0.14)', '#f59e0b', '#fbbf24', '#2dd4bf', 'JetBrains Mono', 'Cascadia Mono', 'tabular-nums']) {
    assert.ok(css.includes(value), value)
  }
})
