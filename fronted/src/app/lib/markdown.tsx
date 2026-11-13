import { Fragment, cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ToolArtifact } from "../api/client";
import { useStreamingText } from "../hooks/useStreamingText";

type Citation = { title: string; date?: string; url?: string };

export function toolCitations(artifacts: ToolArtifact[] = []): Citation[] {
  return artifacts.flatMap((entry) => {
    const rawList = Array.isArray(entry.artifact.items)
      ? entry.artifact.items
      : Array.isArray(entry.artifact.results)
        ? entry.artifact.results
        : [];
    return rawList.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const rec = item as Record<string, unknown>;
      const title = rec.title || rec.source;
      const date = rec.as_of || rec.time || rec.published_date || rec.date;
      const url = typeof rec.url === "string" ? rec.url : undefined;
      return title ? [{ title: String(title), date: date ? String(date) : undefined, url }] : [];
    });
  });
}

function cite(children: ReactNode, citations: Citation[]): ReactNode {
  if (!citations.length) return children;
  if (Array.isArray(children)) return children.map((child, i) => <Fragment key={i}>{cite(child, citations)}</Fragment>);
  if (isValidElement<{ children?: ReactNode }>(children)) {
    if (children.type === "code" || children.type === "a" || children.props?.children == null) return children;
    return cloneElement(children as ReactElement<{ children?: ReactNode }>, undefined, cite(children.props.children, citations));
  }
  if (typeof children !== "string") return children;
  const parts = children.split(/(\[\d{1,2}\])/);
  return parts.map((part, i) => {
    const match = /^\[(\d{1,2})\]$/.exec(part);
    const source = match ? citations[Number(match[1]) - 1] : null;
    if (!source) return <Fragment key={i}>{part}</Fragment>;
    const tip = [source.title, source.date].filter(Boolean).join(" · ");
    const style = "ml-0.5 rounded bg-[var(--gold-muted)] px-1 align-super text-[11px] text-gold";
    return source.url && /^https?:\/\//i.test(source.url)
      ? <a key={i} href={source.url} target="_blank" rel="noreferrer" title={tip} aria-label={tip} className={style}>{part}</a>
      : <span key={i} title={tip} tabIndex={0} aria-label={tip} className={style}>{part}</span>;
  });
}

export function StructuredText({ content, className = "", streaming = false, citations = [] }: {
  content: string; className?: string; streaming?: boolean; citations?: Citation[];
}) {
  const visible = useStreamingText(content, streaming);
  return (
    <div className={"workbench-copy break-words space-y-2 " + className + (streaming ? " stream-tail" : "")}>
      <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
        p: ({ children }) => <p className="whitespace-pre-wrap">{cite(children, citations)}</p>,
        ul: ({ children }) => <ul className="ml-5 list-disc space-y-1">{children}</ul>,
        ol: ({ children }) => <ol className="ml-5 list-decimal space-y-1">{children}</ol>,
        li: ({ children }) => <li>{cite(children, citations)}</li>,
        img: () => null,
        a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer" className="text-gold underline">{children}</a>,
        table: ({ children }) => <div className="overflow-x-auto rounded border"><table className="financial-table text-sm">{children}</table></div>,
        pre: ({ children }) => <pre className="overflow-x-auto rounded bg-secondary p-2">{children}</pre>,
        code: ({ children }) => <code className="font-mono">{children}</code>,
      }}>{visible}</Markdown>
    </div>
  );
}
