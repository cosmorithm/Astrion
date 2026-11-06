import assert from 'node:assert/strict'
import { reportData, reportRelation } from './researchCompare.js'
const a = { run_id:'a', task:'A', result:{status:'completed', answer:'# A'}, usage:{calls:1,unknown_calls:0,totals:{total_tokens:10}}, usage_including_root_review:{calls:2,unknown_calls:0,totals:{total_tokens:20}} }
const b = { run_id:'b', parent_run_id:'a', result:{status:'incomplete_plan',answer:'# B'}, duration_seconds: 1.25 }
assert.equal(reportRelation(a,b),'右侧版本直接续写自左侧。')
assert.equal(reportRelation(b,a),'左侧版本直接续写自右侧。')
assert.match(reportRelation(a, { run_id:'c', parent_run_id:'b' }), /不是已记录的直接父子/)
assert.match(reportRelation(a,a), /同一版本/)
assert.equal(reportRelation(null,b),'')
assert.equal(reportData(a).tokens,'20（含审查）')
assert.equal(reportData({usage:a.usage}).tokens,'10（研究阶段）')
assert.equal(reportData({usage:{calls:2, unknown_calls:1, totals:{total_tokens:5}}}).tokens,'已知 5（另有 1 次未知）（研究阶段）')
assert.equal(reportData({usage:{calls:1, unknown_calls:1, totals:{total_tokens:0}}}).tokens,'未知')
assert.equal(reportData({status:'error'}).status,'error')
assert.equal(reportData({}).answer,'')
assert.equal(reportData({recorded_at:'invalid'}).date,'未知')
assert.equal(reportData({duration_seconds:NaN}).duration,'未知')
assert.equal(reportData(b).answer,'# B')
console.log('researchCompare tests passed')
