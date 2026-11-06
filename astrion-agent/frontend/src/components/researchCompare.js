export function reportData(run) {
  const result = run?.result || {}
  const usage = run?.usage_including_root_review || run?.usage
  const calls = usage?.calls
  const unknown = usage?.unknown_calls || 0
  const total = usage?.totals?.total_tokens
  const tokenText = !usage || calls == null || calls === unknown || typeof total !== 'number'
    ? '未知' : `${unknown ? '已知 ' : ''}${total}${unknown ? `（另有 ${unknown} 次未知）` : ''}${run?.usage_including_root_review ? '（含审查）' : '（研究阶段）'}`
  const rawDate = run?.recorded_at
  const parsed = rawDate ? new Date(rawDate) : null
  return {
    task: run?.task || '未记录',
    status: result.status || run?.status || '未知',
    date: parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : '未知',
    duration: typeof run?.duration_seconds === 'number' && Number.isFinite(run.duration_seconds)
      ? `${run.duration_seconds.toFixed(1)} 秒` : '未知',
    tokens: tokenText,
    parent: run?.parent_run_id || '无或未记录',
    answer: typeof result.answer === 'string' ? result.answer : '',
  }
}
export function reportRelation(a, b) {
  if (!a || !b) return ''
  if (a.run_id === b.run_id) return '当前选择的是同一版本。'
  if (b.parent_run_id === a.run_id) return '右侧版本直接续写自左侧。'
  if (a.parent_run_id === b.run_id) return '左侧版本直接续写自右侧。'
  return '两版不是已记录的直接父子关系；可能来自不同分支，也可能间隔多个版本。'
}
