const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }
const escapeHtml = text => text.replace(/[&<>"']/g, char => entities[char])
const inline = text => text.replace(/`([^`]+)`|\*\*((?:`[^`]+`|[^*`])+)\*\*/g, (_, code, bold) =>
  code === undefined
    ? '<strong>' + bold.replace(/`([^`]+)`/g, '<code>$1</code>') + '</strong>'
    : '<code>' + code + '</code>')

const heading = /^\s*(#{1,6})\s+(.+)$/
const bullet = /^\s*[-*+]\s+(.+)$/
const numbered = /^\s*\d+[.)]\s+(.+)$/
const cells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim())

function isTable(lines, index) {
  const header = lines[index]
  const divider = lines[index + 1]
  return header.includes('|') && divider?.includes('|') &&
    cells(header).length === cells(divider).length && cells(divider).every(cell => /^:?-{3,}:?$/.test(cell))
}

export function assistantMarkdown(text) {
  // Only fixed tags below can enter the returned HTML.
  const lines = escapeHtml(text).split(/\r\n?|\n/)
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i++
      continue
    }
    const title = line.match(heading)
    if (title) {
      const level = title[1].length
      blocks.push(`<h${level}>${inline(title[2])}</h${level}>`)
      i++
      continue
    }
    const list = numbered.test(line) ? numbered : bullet
    if (list.test(line)) {
      const tag = list === numbered ? 'ol' : 'ul'
      const items = []
      while (i < lines.length && list.test(lines[i])) {
        items.push('<li>' + inline(lines[i++].match(list)[1]) + '</li>')
      }
      blocks.push(`<${tag}>${items.join('')}</${tag}>`)
      continue
    }
    if (isTable(lines, i)) {
      const header = cells(line)
      const row = (values, tag) => '<tr>' + values.map(value => `<${tag}>${inline(value)}</${tag}>`).join('') + '</tr>'
      const rows = []
      i += 2
      while (lines[i]?.includes('|') && cells(lines[i]).length === header.length) {
        rows.push(row(cells(lines[i++]), 'td'))
      }
      blocks.push('<div class="answer-table"><table><thead>' + row(header, 'th') +
        '</thead><tbody>' + rows.join('') + '</tbody></table></div>')
      continue
    }
    const paragraph = [inline(line)]
    i++
    while (lines[i]?.trim() && !heading.test(lines[i]) && !bullet.test(lines[i]) &&
           !numbered.test(lines[i]) && !isTable(lines, i)) {
      paragraph.push(inline(lines[i++]))
    }
    blocks.push('<p>' + paragraph.join('<br>') + '</p>')
  }
  return blocks.join('')
}
