/* A small Markdown renderer for the bundled strategy summary (headings, paragraphs, lists, tables, quotes, rules,
   **bold**, *italic*, `code`). Text only: no HTML is ever injected. */
import type { ReactNode } from "react";

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  let last = 0, k = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("**")) out.push(<strong key={`${key}-${k++}`}>{t.slice(2, -2)}</strong>);
    else if (t.startsWith("`")) out.push(<code key={`${key}-${k++}`} className="mono">{t.slice(1, -1)}</code>);
    else out.push(<em key={`${key}-${k++}`}>{t.slice(1, -1)}</em>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text, testId }: { text: string; testId?: string }) {
  const lines = text.replace(/\r/g, "").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0, b = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${b++}`;
    if (!line.trim()) { i++; continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = h[1].length;
      const content = inline(h[2], key);
      blocks.push(lvl === 1 ? <h1 key={key}>{content}</h1> : lvl === 2 ? <h2 key={key}>{content}</h2>
        : lvl === 3 ? <h3 key={key}>{content}</h3> : <h4 key={key}>{content}</h4>);
      i++; continue;
    }
    if (/^---+\s*$/.test(line)) { blocks.push(<hr key={key} />); i++; continue; }
    if (line.startsWith(">")) {
      const q: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) q.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(<blockquote key={key}>{inline(q.join(" "), key)}</blockquote>);
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-+:?$/.test(c))) rows.push(cells);
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <div key={key} className="table-wrap"><table className="dense md-table">
          <thead><tr>{head.map((c, k) => <th key={k}>{inline(c, `${key}h${k}`)}</th>)}</tr></thead>
          <tbody>{body.map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c, `${key}${k}-${j}`)}</td>)}</tr>)}</tbody>
        </table></div>);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: { depth: number; text: string }[] = [];
      while (i < lines.length && (/^\s*[-*]\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        const l = lines[i];
        const m2 = /^(\s*)[-*]\s+(.*)$/.exec(l);
        if (m2) items.push({ depth: Math.floor(m2[1].length / 2), text: m2[2] });
        else items[items.length - 1].text += " " + l.trim();
        i++;
      }
      blocks.push(<ul key={key}>{items.map((it, k) => (
        <li key={k} style={it.depth ? { marginLeft: it.depth * 18 } : undefined}>{inline(it.text, `${key}-${k}`)}</li>))}</ul>);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|>|\||---|\s*[-*]\s)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);           // never loop on a line no rule above took
    blocks.push(<p key={key}>{inline(para.join(" "), key)}</p>);
  }
  return <div className="markdown" data-testid={testId}>{blocks}</div>;
}
