import { useRef, useState } from "react";
import type { ReportDoc } from "../../api/client";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { copyPoster, renderPoster, savePoster } from "./sharePoster";

function excerpt(text: string, limit: number): string {
  const chars = Array.from(text.trim());
  return chars.length <= limit ? text.trim() : chars.slice(0, limit).join("") + "…";
}

export function SharePosterView({ open, onOpenChange, report, name }: {
  open: boolean; onOpenChange: (open: boolean) => void; report: ReportDoc; name: string;
}) {
  const node = useRef<HTMLElement>(null);
  const [large, setLarge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  async function exportImage(copy: boolean) {
    if (!node.current || busy) return;
    setBusy(true);
    setStatus("");
    setError("");
    try {
      if (copy) await copyPoster(node.current);
      else savePoster(await renderPoster(node.current), name);
      setStatus(copy ? "已复制图片，可在微信中粘贴。" : "已生成 PNG 文件。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "长图导出失败。");
    } finally {
      setBusy(false);
    }
  }

  return <Dialog open={open} onOpenChange={(value) => { if (!busy) onOpenChange(value); }}>
    <DialogContent className="flex max-h-[90dvh] flex-col sm:max-w-[760px]">
      <DialogHeader><DialogTitle>微信分享长图</DialogTitle><DialogDescription>包含研报摘要、关键事实、数据日期和风险提示。</DialogDescription></DialogHeader>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button type="button" disabled={busy} aria-pressed={!large} className="rounded border px-2 py-1 aria-pressed:bg-accent" onClick={() => setLarge(false)}>标准版</button>
        <button type="button" disabled={busy} aria-pressed={large} className="rounded border px-2 py-1 aria-pressed:bg-accent" onClick={() => setLarge(true)}>关怀大字版</button>
        <button type="button" disabled={busy} className="ml-auto rounded border px-2 py-1 text-gold" onClick={() => void exportImage(false)}>保存高清 PNG</button>
        <button type="button" disabled={busy} className="rounded bg-action px-2 py-1 text-gold-foreground" onClick={() => void exportImage(true)}>复制到剪贴板</button>
      </div>
      {(status || busy) && <p role="status">{busy ? "正在生成长图…" : status}</p>}
      {error && <p role="alert" className="text-destructive">{error}</p>}
      <div className="min-h-0 overflow-auto rounded border">
        <article ref={node} className="w-[650px] space-y-5 whitespace-pre-wrap break-words p-8"
          style={{ background: "#FAF8F3", color: "#27241D", fontFamily: '"Noto Serif SC", "Songti SC", SimSun, serif',
            fontSize: large ? 25 : 19, lineHeight: 1.6 }}>
          <header className="border-b-2 pb-4" style={{ borderColor: "#8B6528" }}>
            <p style={{ color: "#77551E", fontSize: large ? 22 : 17 }}>景渊 · 投研摘要</p>
            <h2 style={{ fontSize: large ? 46 : 38, fontWeight: 700, lineHeight: 1.28 }}>{name}</h2>
            <p style={{ color: "#5E5544", fontSize: large ? 20 : 16 }}>数据时点 {report.as_of || "—"}</p>
          </header>
          <section><h3 style={{ color: "#77551E", fontWeight: 700 }}>摘要</h3><p>{excerpt(report.summary || "", 430)}</p></section>
          {report.sections
            ?.filter((section) => section.title !== "客观风险指标" && section.title !== "证据分层与数据来源")
            .slice(0, 5)
            .map((section, index) => <section key={index} className="border-t pt-3" style={{ borderColor: "#DED2BA" }}>
            <h3 style={{ color: "#77551E", fontWeight: 700 }}>{section.title}</h3><p>{excerpt(section.body, 290)}</p>
          </section>)}
          {!!report.risks?.length && <section><h3 style={{ color: "#77551E", fontWeight: 700 }}>风险提示</h3>
            {report.risks.slice(0, 4).map((risk, index) => <p key={index}>· {excerpt(risk, 120)}</p>)}</section>}
          {!!report.sources?.length && <p style={{ color: "#5E5544" }}>资料来源 {report.sources.slice(0, 4).map((source) => source.title).join("、")}</p>}
          <p className="border-t pt-3" style={{ borderColor: "#DED2BA", color: "#5E5544", fontSize: large ? 19 : 15 }}>{report.disclaimer}</p>
        </article>
      </div>
    </DialogContent>
  </Dialog>;
}
