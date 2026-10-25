/**
 * Run from frontend/: node --experimental-strip-types --test src/lib/prepareAssistantMarkdown.test.ts
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import remarkBreaks from 'remark-breaks'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { closeOpenMarkdown, prepareAssistantMarkdown, stripInternalTags } from './prepareAssistantMarkdown.ts'

describe('stripInternalTags', () => {
  it('removes protocol tags and plan fences', () => {
    const raw = 'Hello\n\n<edit_plan>{"a":1}</edit_plan>\n<search_request>{"terms":["x"]}</search_request>\n```plan_questions\n{"questions":[]}\n```'
    assert.equal(stripInternalTags(raw), 'Hello')
  })
  it('hides an unfinished fence while streaming', () => {
    const raw = 'Hi\n```plan_questions\n{"que'
    assert.equal(stripInternalTags(raw, true), 'Hi')
  })
})

describe('closeOpenMarkdown', () => {
  it('closes an open fence and an open bold marker', () => {
    assert.equal(closeOpenMarkdown('See\n```ts\nconst x = 1'), 'See\n```ts\nconst x = 1\n```')
    assert.equal(closeOpenMarkdown('This is **partial'), 'This is **partial**')
  })
  it('leaves text inside a finished fence alone', () => {
    const fenced = 'Before\n```js\nconst s = "**not bold"\n```\nAfter'
    assert.equal(closeOpenMarkdown(fenced), fenced)
  })
})

describe('prepareAssistantMarkdown', () => {
  it('does not invent structure in a finished reply', () => {
    const text = 'One sentence. Another sentence.'
    assert.equal(prepareAssistantMarkdown(text), text)
  })
  it('keeps a single newline so remark-breaks can turn it into a line break', async () => {
    const text = 'First line\nSecond line'
    assert.equal(prepareAssistantMarkdown(text), text)
    const processor = unified().use(remarkParse).use(remarkBreaks)
    const tree = await processor.run(processor.parse(text)) as unknown as {
      children: Array<{ children?: Array<{ type: string }> }>
    }
    const types = (tree.children[0].children || []).map((n) => n.type)
    assert.ok(types.includes('break'))
  })
  it('repairs markdown only while streaming', () => {
    assert.equal(prepareAssistantMarkdown('**open', { streaming: true }), '**open**')
    assert.equal(prepareAssistantMarkdown('**open'), '**open')
  })
})
