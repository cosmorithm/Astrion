// Layout only persisted parent_run_id relationships; never infer dependencies from time.
export function layoutResearchRuns(data) {
  const nodes = Array.isArray(data?.nodes) ? data.nodes.filter(n => n && typeof n.run_id === 'string') : []
  const byId = new Map(nodes.map(n => [n.run_id, n]))
  const children = new Map(nodes.map(n => [n.run_id, []]))
  const degree = new Map(nodes.map(n => [n.run_id, 0]))
  const missing = new Set()
  for (const n of nodes) {
    if (n.parent_invalid) { missing.add(n.run_id); continue }
    if (!n.parent_run_id) continue
    if (!byId.has(n.parent_run_id)) { missing.add(n.run_id); continue }
    children.get(n.parent_run_id).push(n.run_id)
    degree.set(n.run_id, 1)
  }
  const depths = new Map()
  const queue = nodes.filter(n => degree.get(n.run_id) === 0).map(n => n.run_id)
  for (const id of queue) depths.set(id, 0)
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const parent = queue[cursor]
    for (const child of children.get(parent)) {
      degree.set(child, degree.get(child) - 1)
      if (degree.get(child) === 0) { depths.set(child, depths.get(parent) + 1); queue.push(child) }
    }
  }
  const unresolved = new Set(nodes.filter(n => !depths.has(n.run_id)).map(n => n.run_id))
  const rows = new Map()
  const positioned = nodes.map(node => {
    const depth = depths.get(node.run_id) ?? 0
    const row = rows.get(depth) || 0
    rows.set(depth, row + 1)
    return { ...node, depth, row, missingParent: missing.has(node.run_id), invalidRelation: unresolved.has(node.run_id),
      x: 20 + depth * 250, y: 20 + row * 102 }
  })
  const positionedById = new Map(positioned.map(n => [n.run_id, n]))
  const edges = positioned.flatMap(n => {
    const parent = positionedById.get(n.parent_run_id)
    return parent && !n.invalidRelation && !parent.invalidRelation && n.depth === parent.depth + 1
      ? [{ id: `${parent.run_id}:${n.run_id}`, x1: parent.x + 210, y1: parent.y + 40, x2: n.x, y2: n.y + 40 }]
      : []
  })
  return { nodes: positioned, edges, missingCount: missing.size, invalidCount: unresolved.size,
    width: Math.max(250, ...positioned.map(n => n.x + 230)),
    height: Math.max(110, ...positioned.map(n => n.y + 100)) }
}
