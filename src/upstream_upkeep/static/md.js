// Small, safe Markdown renderer for chat replies (no dependencies).
//
// Everything is HTML-escaped first, so model output can never inject markup;
// only the constructs below are turned back into tags. Links are kept only for
// http(s) URLs. Supported: ``` fences, `code`, **bold**, *italic*, # headings,
// - / * / 1. lists, > quotes, [text](url), paragraphs and line breaks.
(function (root) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")

  function inline(text) {
    const codes = []
    // Code spans as in CommonMark: a run of N backticks closes on the next run of
    // exactly N, so `` `x` `` shows `x`; one space just inside each end is trimmed.
    let s = esc(text).replace(/(`+)(.+?)(?<!`)\1(?!`)/g, (_, ticks, c) => {
      if (c.length > 2 && c.startsWith(" ") && c.endsWith(" ") && c.trim()) c = c.slice(1, -1)
      codes.push(c)
      return `\u0000${codes.length - 1}\u0000`
    })
    s = s
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)"]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`)
  }

  function renderMarkdown(src) {
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n")
    const out = []
    let para = []
    let list = null  // {tag, items}
    const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = [] } }
    const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null } }
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]
      const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/)
      if (fence) {
        flushPara(); flushList()
        const body = []
        while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i])
        const lang = fence[1] ? ` data-lang="${esc(fence[1])}"` : ""
        out.push(`<pre class="md-code"${lang}><code>${esc(body.join("\n"))}</code></pre>`)
        continue
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/)
      const ul = line.match(/^\s*[-*]\s+(.*)$/)
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/)
      const q = line.match(/^>\s?(.*)$/)
      if (h) { flushPara(); flushList(); const n = Math.min(h[1].length + 2, 6); out.push(`<h${n}>${inline(h[2])}</h${n}>`) }
      else if (ul || ol) {
        flushPara()
        const tag = ul ? "ul" : "ol"
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] } }
        list.items.push((ul || ol)[1])
      }
      else if (q) { flushPara(); flushList(); out.push(`<blockquote>${inline(q[1])}</blockquote>`) }
      else if (!line.trim()) { flushPara(); flushList() }
      else { flushList(); para.push(line) }
    }
    flushPara(); flushList()
    return out.join("")
  }

  root.renderMarkdown = renderMarkdown
  if (typeof module !== "undefined") module.exports = { renderMarkdown }
})(typeof window !== "undefined" ? window : globalThis)
