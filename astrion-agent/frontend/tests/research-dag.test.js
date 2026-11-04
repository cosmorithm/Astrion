import test from 'node:test'
import assert from 'node:assert/strict'
import { layoutResearchRuns } from '../src/components/researchDag.js'

test('draws exactly the persisted parent links, including branches', () => {
  const graph = layoutResearchRuns({ nodes: [
    { run_id: 'root', parent_run_id: null },
    { run_id: 'left', parent_run_id: 'root' },
    { run_id: 'right', parent_run_id: 'root' },
    { run_id: 'leaf', parent_run_id: 'left' },
    { run_id: 'separate', parent_run_id: null },
  ] })
  assert.deepEqual(graph.edges.map(edge => edge.id), ['root:left', 'root:right', 'left:leaf'])
  assert.deepEqual(graph.nodes.map(node => node.depth), [0, 1, 1, 2, 0])
  assert.equal(graph.missingCount, 0)
  assert.equal(graph.invalidCount, 0)
})

test('keeps missing parent nodes visible but never fabricates an edge', () => {
  const graph = layoutResearchRuns({ nodes: [{ run_id: 'child', parent_run_id: 'unseen' }] })
  assert.equal(graph.nodes.length, 1)
  assert.equal(graph.nodes[0].missingParent, true)
  assert.deepEqual(graph.edges, [])
})

test('marks malformed parent references even when no parent string is available', () => {
  const graph = layoutResearchRuns({ nodes: [{ run_id: 'child', parent_run_id: null, parent_invalid: true }] })
  assert.equal(graph.missingCount, 1)
  assert.equal(graph.nodes[0].missingParent, true)
  assert.deepEqual(graph.edges, [])
})

test('keeps cyclic nodes and dependent nodes visible without drawing invalid edges', () => {
  const graph = layoutResearchRuns({ nodes: [
    { run_id: 'a', parent_run_id: 'b' }, { run_id: 'b', parent_run_id: 'a' },
    { run_id: 'c', parent_run_id: 'a' }, { run_id: 'ok', parent_run_id: null },
  ] })
  assert.equal(graph.nodes.length, 4)
  assert.equal(graph.invalidCount, 3)
  assert.deepEqual(graph.edges, [])
  assert.equal(graph.nodes.find(node => node.run_id === 'ok').invalidRelation, false)
})

test('empty and legacy entries do not crash the graph', () => {
  assert.equal(layoutResearchRuns(null).nodes.length, 0)
  const graph = layoutResearchRuns({ nodes: [null, {}, { run_id: 'legacy' }] })
  assert.equal(graph.nodes.length, 1)
  assert.equal(graph.nodes[0].depth, 0)
})
