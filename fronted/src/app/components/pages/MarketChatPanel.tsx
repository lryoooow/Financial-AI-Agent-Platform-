import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent } from "react";
import { ImagePlus, Loader2, MessageSquare, Send, X } from "lucide-react";
import { api, type ChatMessage, type LlmConfig, type MarketContext } from "../../api/client";
import type { TechnicalIndicator } from "../charts/KLineChart";
import { ModelPicker } from "../ModelPicker";

interface ActiveModel { provider: string; model: string; accent: string; }
const STORAGE_KEY = "finresearch_market_conversations";

function conversationMap(): Record<string, string> {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch { return {}; }
}

function readPreview(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export function MarketChatPanel({
  code,
  kind,
  indicator,
  visibleRange,
  activeModel,
  onGoSettings,
}: {
  code: string;
  kind: "intraday" | "kline";
  indicator: TechnicalIndicator;
  visibleRange: { start: string; end: string };
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
}) {
  const [conversationId, setConversationId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [images, setImages] = useState<{ file: File; preview: string }[]>([]);
  const [llm, setLlm] = useState<LlmConfig | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // 当前股票代码的同步镜像：切股后旧股票仍在流式中的 done/error 不得覆盖新股票的面板
  const codeRef = useRef(code);
  useEffect(() => {
    codeRef.current = code;
  }, [code]);

  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    setError("");
    const saved = conversationMap()[code];
    if (!saved) {
      setConversationId("");
      return;
    }
    api.chatConversation(saved)
      .then((payload) => {
        if (!cancelled) {
          setConversationId(saved);
          setMessages(payload.messages);
        }
      })
      .catch(() => {
        const map = conversationMap();
        delete map[code];
        localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
        if (!cancelled) setConversationId("");
      });
    return () => { cancelled = true; };
  }, [code]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages, status]);

  const context = useMemo<MarketContext>(() => ({
    code,
    kind,
    adjust: "qfq",
    visible_start: kind === "kline" ? visibleRange.start || undefined : undefined,
    visible_end: kind === "kline" ? visibleRange.end || undefined : undefined,
    indicator,
  }), [code, indicator, kind, visibleRange.end, visibleRange.start]);

  const addFiles = async (files: File[]) => {
    const accepted = files.filter((file) => ["image/jpeg", "image/png", "image/webp"].includes(file.type));
    if (accepted.length !== files.length) setError("仅支持 JPEG、PNG、WebP 图片。");
    const remaining = Math.max(0, 4 - images.length);
    const prepared = await Promise.all(accepted.slice(0, remaining).map(async (file) => ({ file, preview: await readPreview(file) })));
    setImages((current) => [...current, ...prepared].slice(0, 4));
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (files.length) void addFiles(files);
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    void addFiles(Array.from(event.dataTransfer.files));
  };

  const ensureConversation = async (): Promise<string> => {
    if (conversationId) return conversationId;
    const payload = await api.createChatConversation(`${code} 行情分析`, llm);
    const id = payload.conversation.id;
    const map = conversationMap();
    map[code] = id;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
    setConversationId(id);
    return id;
  };

  const send = async () => {
    const message = text.trim();
    if ((!message && !images.length) || streaming) return;
    setStreaming(true);
    setError("");
    setStatus("准备行情上下文");
    const optimisticId = `local-${Date.now()}`;
    const assistantId = `${optimisticId}-assistant`;
    setMessages((current) => [
      ...current,
      { id: optimisticId, role: "user", content: message, images: images.map((item) => item.preview), created_at: new Date().toISOString() },
      { id: assistantId, role: "assistant", content: "", created_at: new Date().toISOString() },
    ]);
    setText("");
    const pendingImages = images;
    setImages([]);
    const codeAtSend = code;
    let id = conversationId;
    try {
      id = await ensureConversation();
      const uploaded = await Promise.all(pendingImages.map((item) => api.uploadChatImage(item.file)));
      await api.sendChatMessageStream(id, message, llm, undefined, {
        imageAssetIds: uploaded.map((item) => item.asset_id),
        marketContext: context,
        onStage: (_stage, label) => setStatus(label),
        onWarning: (warning) => setError(warning),
        onToken: (token) => setMessages((current) => current.map((item) => item.id === assistantId ? { ...item, content: item.content + token } : item)),
        onDone: (payload) => {
          setStatus("");
          if (codeRef.current !== codeAtSend) return; // 用户已切股，旧流不覆盖新面板
          setMessages(payload.messages);
        },
        onError: (messageText) => {
          setError(messageText);
          setStatus("");
          if (codeRef.current !== codeAtSend) return;
          void api.chatConversation(id).then((payload) => {
            if (codeRef.current === codeAtSend) setMessages(payload.messages);
          }).catch(() => {});
        },
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      if (id && codeRef.current === codeAtSend) {
        try {
          const payload = await api.chatConversation(id);
          if (codeRef.current === codeAtSend) setMessages(payload.messages);
        } catch {}
      }
    } finally {
      setStreaming(false);
      setStatus("");
    }
  };

  return (
    <div className="flex min-h-[430px] flex-col overflow-hidden rounded-xl border border-border bg-card" onDragOver={(event) => event.preventDefault()} onDrop={onDrop}>
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2.5">
        <div className="flex items-center gap-2 text-sm"><MessageSquare className="size-4 text-gold" /> 图表对话</div>
        <ModelPicker active={activeModel} onChange={setLlm} onGoSettings={onGoSettings} />
      </div>
      <div ref={listRef} className="min-h-[210px] flex-1 space-y-2 overflow-y-auto p-3">
        {!messages.length && <div className="rounded-lg bg-secondary/40 p-3 text-xs leading-relaxed text-muted-foreground">可直接询问当前 {kind === "kline" ? "日 K 与技术指标" : "分时走势"}。系统只传标的和可见区间，Agent 会重新调用真实行情工具核验数值。</div>}
        {messages.map((message) => (
          <div key={message.id} className={`max-w-[92%] rounded-lg px-2.5 py-2 text-xs leading-relaxed ${message.role === "user" ? "ml-auto bg-gold/15 text-foreground" : "bg-secondary text-foreground/90"}`}>
            {(message.images?.length || message.image_asset_ids?.length) ? <div className="mb-1.5 flex gap-1 overflow-x-auto">{[...(message.images || []), ...(message.image_asset_ids || []).map(api.imageAssetUrl)].map((url, index) => <img key={`${url}-${index}`} src={url} className="size-14 rounded object-cover" />)}</div> : null}
            <div className="whitespace-pre-wrap">{message.content || (message.role === "assistant" && streaming ? "…" : "[图片]")}</div>
          </div>
        ))}
        {status && <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground"><Loader2 className="size-3 animate-spin text-gold" />{status}</div>}
      </div>
      <div className="border-t border-border p-2.5">
        {images.length > 0 && <div className="mb-2 flex gap-1.5 overflow-x-auto">{images.map((item, index) => <div key={`${item.file.name}-${index}`} className="relative shrink-0"><img src={item.preview} className="size-14 rounded-md border border-border object-cover" /><button onClick={() => setImages((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="absolute -right-1 -top-1 rounded-full bg-popover p-0.5 shadow"><X className="size-3" /></button></div>)}</div>}
        {error && <p className="mb-2 text-[10px] leading-relaxed text-destructive">{error}</p>}
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          onPaste={onPaste}
          onKeyDown={(event) => {
            // isComposing：中文输入法敲 Enter 选字/上屏时不能把拼音半成品发出去
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
          placeholder="结合当前图表提问，支持粘贴图片…"
          rows={3}
          className="w-full resize-none rounded-lg bg-input px-3 py-2 text-xs outline-none placeholder:text-muted-foreground focus:ring-1 focus:ring-gold/40"
        />
        <div className="mt-2 flex items-center justify-between">
          <div><input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={(event) => void addFiles(Array.from(event.target.files || []))} /><button onClick={() => fileRef.current?.click()} disabled={images.length >= 4 || streaming} className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-40"><ImagePlus className="size-4" /></button></div>
          <button onClick={() => void send()} disabled={streaming || (!text.trim() && !images.length)} className="flex items-center gap-1.5 rounded-md bg-gold px-2.5 py-1.5 text-xs text-[#17140a] disabled:opacity-40">{streaming ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}发送</button>
        </div>
      </div>
    </div>
  );
}
