import { parseDatasetFile } from '@/utils/datasetFile'

type Case = { name: string; file: string; text: string; expectOk: boolean; expectCount?: number; expectFirst?: Record<string, unknown>; expectErrIncludes?: string; expectWarn?: boolean }
const cases: Case[] = [
  { name: 'CSV 兼容', file: 'a.csv', text: 'question,ground_truth,stratum\nQ1,A1,easy\nQ2,A2,\n', expectOk: true, expectCount: 2 },
  { name: 'JSON 数组', file: 'a.json', text: JSON.stringify([{ question: 'Q1', ground_truth: 'A1' }, { question: 'Q2', ground_truth: 'A2' }]), expectOk: true, expectCount: 2 },
  { name: 'JSON items 包装', file: 'a.json', text: JSON.stringify({ name: 'x', items: [{ question: 'Q1', ground_truth: 'A1' }] }), expectOk: true, expectCount: 1 },
  { name: '单对象', file: 'a.json', text: JSON.stringify({ question: 'Q1', ground_truth: 'A1' }), expectOk: true, expectCount: 1 },
  { name: '中文字段名', file: 'a.json', text: JSON.stringify([{ '问题': 'Q1', '标准答案': 'A1' }]), expectOk: true, expectCount: 1 },
  { name: '驼峰+全量字段', file: 'a.json', text: JSON.stringify([{ query: 'Q1', referenceAnswer: 'A1', stratum: 'hard', difficulty: 3, reference_contexts: ['c1'], item_type: 'rag_qa' }]), expectOk: true, expectCount: 1, expectFirst: { question: 'Q1', reference_answer: 'A1', stratum: 'hard', difficulty: 3, item_type: 'rag_qa' } },
  { name: '缺 ground_truth 拒绝', file: 'a.json', text: JSON.stringify([{ question: 'Q1' }]), expectOk: false, expectErrIncludes: 'ground_truth 为空' },
  { name: '非法 item_type 仅提示', file: 'a.json', text: JSON.stringify([{ question: 'Q1', ground_truth: 'A1', item_type: 'bogus' }]), expectOk: true, expectCount: 1, expectWarn: true },
  { name: 'JSONL', file: 'a.jsonl', text: '{"question":"Q1","ground_truth":"A1"}\n{"question":"Q2","ground_truth":"A2"}\n', expectOk: true, expectCount: 2 },
  { name: 'JSONL 存成 .json 自动兜底', file: 'a.json', text: '{"question":"Q1","ground_truth":"A1"}\n{"question":"Q2","ground_truth":"A2"}', expectOk: true, expectCount: 2 },
  { name: '坏 JSON 报错', file: 'a.json', text: '[{question: Q1}]', expectOk: false, expectErrIncludes: 'JSON 解析失败' },
  { name: '不支持的扩展名', file: 'a.txt', text: 'x', expectOk: false, expectErrIncludes: '仅支持' },
  { name: '空数组', file: 'a.json', text: '[]', expectOk: false, expectErrIncludes: '未解析到有效样本' },
]

let failed = 0
for (const c of cases) {
  const r = parseDatasetFile(c.text, c.file)
  const problems: string[] = []
  if (r.ok !== c.expectOk) problems.push(`ok=${r.ok} 期望 ${c.expectOk}（errors=${JSON.stringify(r.errors)}）`)
  if (c.expectCount !== undefined && r.items.length !== c.expectCount) problems.push(`条数 ${r.items.length} 期望 ${c.expectCount}`)
  if (c.expectErrIncludes && !r.errors.join('|').includes(c.expectErrIncludes)) problems.push(`错误文案缺「${c.expectErrIncludes}」实际 ${JSON.stringify(r.errors)}`)
  if (c.expectWarn && r.warnings.length === 0) problems.push('期望有 warning 实际没有')
  if (c.expectFirst) {
    for (const [k, v] of Object.entries(c.expectFirst)) {
      const got = (r.items[0] as Record<string, unknown>)[k]
      if (JSON.stringify(got) !== JSON.stringify(v)) problems.push(`字段 ${k}=${JSON.stringify(got)} 期望 ${JSON.stringify(v)}`)
    }
  }
  if (problems.length) {
    failed++
    console.log(`FAIL  ${c.name}: ${problems.join('; ')}`)
  } else {
    console.log(`ok    ${c.name}`)
  }
}
console.log(failed === 0 ? `\n全部 ${cases.length} 个用例通过` : `\n${failed}/${cases.length} 失败`)
process.exit(failed === 0 ? 0 : 1)
