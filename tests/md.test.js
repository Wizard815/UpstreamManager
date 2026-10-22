// Tests for static/md.js. Run: node tests/md.test.js (also run by pytest).
const assert = require("assert")
const { renderMarkdown: md } = require("../src/upstream_upkeep/static/md.js")

const cases = [
  ["escapes raw HTML", md("<script>alert(1)</script>"), (h) => !h.includes("<script") && h.includes("&lt;script&gt;")],
  ["escapes HTML inside code", md("`<img src=x onerror=alert(1)>`"), (h) => h.includes("<code>&lt;img") && !h.includes("<img")],
  ["escapes HTML inside fences", md("```html\n<b>x</b>\n```"), (h) => h.includes("&lt;b&gt;x&lt;/b&gt;") && h.includes('data-lang="html"')],
  ["no javascript: links", md("[click](javascript:alert(1))"), (h) => !h.includes("<a ")],
  ["no attribute breakout in links", md('[x](https://a.b/"onmouseover="alert(1))'), (h) => !/<a [^>]*\sonmouseover=/.test(h)],
  ["http links open safely", md("[docs](https://example.com/a)"), (h) => h.includes('href="https://example.com/a"') && h.includes('rel="noopener noreferrer"')],
  ["bold and italic", md("**bold** and *it*"), (h) => h.includes("<strong>bold</strong>") && h.includes("<em>it</em>")],
  ["inline code keeps stars", md("`a*b*c`"), (h) => h.includes("<code>a*b*c</code>")],
  ["double-backtick spans", md("- `` `63c15e3` `` adds `c` to `a.txt`"), (h) => h.includes("<code>`63c15e3`</code>") && h.includes("<code>c</code>") && h.includes("<code>a.txt</code>")],
  ["lone backtick stays text", md("it's a ` mark"), (h) => h.includes("a ` mark") && !h.includes("<code>")],
  ["lists", md("- one\n- two\n\n1. a\n2. b"), (h) => h.includes("<ul><li>one</li><li>two</li></ul>") && h.includes("<ol><li>a</li><li>b</li></ol>")],
  ["headings are demoted", md("# Title"), (h) => h.includes("<h3>Title</h3>")],
  ["line breaks in paragraphs", md("a\nb"), (h) => h === "<p>a<br>b</p>"],
  ["unclosed fence still renders", md("```\ncode"), (h) => h.includes("<pre") && h.includes("code")],
  ["empty input", md(""), (h) => h === ""],
]
let failed = 0
for (const [name, html, ok] of cases) {
  if (ok(html)) continue
  failed++
  console.error(`FAIL ${name}: ${html}`)
}
assert.strictEqual(failed, 0, `${failed} markdown case(s) failed`)
console.log(`${cases.length} markdown cases passed`)
