import { useEffect, useRef, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  Bot,
  Image as ImageIcon,
  Loader2,
  MessageSquare,
  Plus,
  Send,
  Sparkles,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { api, type ChatConversation, type ChatMessage, type LlmConfig } from "../../api/client";
import { ModelPicker } from "../ModelPicker";
import { StructuredText } from "../../lib/markdown";

/** 图片压缩：canvas resize 到 maxDim、JPEG quality，单图 ~150KB。
 *  控制体积避免 base64 撑爆对话预算；多模态模型对 1280px 已足够清晰。 */
async function compressImage(file: File, maxDim = 1280, quality = 0.82): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        let { width, height } = img;
        if (width > maxDim || height > maxDim) {
          const ratio = Math.min(maxDim / width, maxDim / height);
          width = Math.round(width * ratio);
          height = Math.round(height * ratio);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        if (!ctx) { reject(new Error("canvas 不可用")); return; }
        ctx.drawImage(img, 0, 0, width, height);
        resolve(canvas.toDataURL("image/jpeg", quality));
      };
      img.onerror = () => reject(new Error("图片加载失败"));
      img.src = reader.result as string;
    };
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}

async function dataUrlToBlob(dataUrl: string): Promise<Blob> {
  const response = await fetch(dataUrl);
  return response.blob();
}

/** 阶段摘要文字的打字机滚动动效（逐字浮现 + 光标），比静态文字 + 跳点更具"思考动效"。 */
function Typewriter({ text, className }: { text: string; className?: string }) {
  const [n, setN] = useState(0);
  useEffect(() => {
    setN(0);
    let i = 0;
    const id = setInterval(() => {
      i += 1;
      setN(i);
      if (i >= text.length) clearInterval(id);
    }, 45);
    return () => clearInterval(id);
  }, [text]);
  return (
    <span className={className}>
      {text.slice(0, n)}
      {n < text.length && <span className="opacity-60">▎</span>}
    </span>
  );
}

interface ActiveModel { provider: string; model: string; accent: string; }

export function ChatDesk({
  activeModel,
  onGoSettings,
}: {
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
}) {
  const [conversations, setConversations] = useState<ChatConversation[]>([]);
  const [current, setCurrent] = useState<ChatConversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [llm, setLlm] = useState<LlmConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [stage, setStage] = useState("");
  const [streamingId, setStreamingId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 当前查看会话 id 的同步镜像：SSE 回调闭包拿到的 current 是发送时的旧值，
  // 校验"用户是否已切走会话"必须读 ref，否则旧会话的 done/error 会覆盖新会话界面
  const currentIdRef = useRef<string | null>(null);
  useEffect(() => {
    currentIdRef.current = current?.id ?? null;
  }, [current]);

  // 图片上传状态
  const [pendingImages, setPendingImages] = useState<string[]>([]); // 待发送的 base64 data URL
  const [lightbox, setLightbox] = useState<string | null>(null);   // 全屏预览的图片 URL
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleFiles(files: FileList | File[]) {
    const imgs: string[] = [];
    for (const f of Array.from(files)) {
      if (!f.type.startsWith("image/")) continue;
      try {
        const compressed = await compressImage(f);
        imgs.push(compressed);
      } catch { /* skip unsupported */ }
    }
    if (imgs.length) {
      setPendingImages((prev) => [...prev, ...imgs].slice(0, 4));
    }
  }

  async function refreshList(archived = showArchived) {
    const r = await api.chatConversations(archived);
    setConversations(r.conversations);
  }

  useEffect(() => {
    setError("");
    setCurrent(null);
    setMessages([]);
    refreshList(showArchived).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [showArchived]);

  // 流式跟随只在用户本来就贴底时才发生。曾经每个 token 触发一次 smooth 滚动：
  // 动画被高频打断表现为抖动卡顿，且会把正在上翻历史的用户强行拉回底部。
  const atBottomRef = useRef(true);
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  useEffect(() => {
    if (!atBottomRef.current) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, loading]);

  async function openConversation(id: string) {
    setError("");
    try {
      const r = await api.chatConversation(id);
      atBottomRef.current = true; // 换会话回到贴底跟随，避免沿用旧会话的上翻状态
      setCurrent(r.conversation);
      setMessages(r.messages);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function startConversation() {
    setError("");
    if (showArchived) setShowArchived(false);
    try {
      const r = await api.createChatConversation("新对话", llm);
      setCurrent(r.conversation);
      setMessages([]);
      setConversations((prev) => [r.conversation, ...prev]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function setArchived(c: ChatConversation, archived: boolean) {
    setError("");
    try {
      await api.archiveChatConversation(c.id, archived);
      if (current?.id === c.id) {
        setCurrent(null);
        setMessages([]);
      }
      await refreshList(showArchived);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function deleteConversation(c: ChatConversation) {
    if (!window.confirm(`确定删除「${c.title || "新对话"}」及其全部本地历史吗？`)) return;
    setError("");
    try {
      await api.deleteChatConversation(c.id);
      if (current?.id === c.id) {
        setCurrent(null);
        setMessages([]);
      }
      await refreshList(showArchived);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function send() {
    const text = input.trim();
    const imgs = [...pendingImages];
    if ((!text && !imgs.length) || loading || showArchived || current?.archived_at) return;
    setInput("");
    setPendingImages([]);
    setError("");
    setLoading(true);
    setStage("正在理解问题");
    const optimistic: ChatMessage = {
      id: `local-${Date.now()}`,
      role: "user",
      content: text,
      images: imgs.length ? imgs : undefined,
      created_at: new Date().toISOString(),
    };
    const assistantId = `stream-${Date.now()}`;
    setMessages((m) => [
      ...m,
      optimistic,
      { id: assistantId, role: "assistant", content: "", created_at: new Date().toISOString() },
    ]);
    setStreamingId(assistantId);
    let conversationId = current?.id || "";
    try {
      let conv = current;
      if (!conv) {
        const created = await api.createChatConversation(text.slice(0, 36) || "图片对话", llm);
        conv = created.conversation;
        setCurrent(conv);
        setConversations((prev) => [conv!, ...prev]);
      }
      conversationId = conv.id;
      const assets = await Promise.all(imgs.map(async (image) => api.uploadChatImage(await dataUrlToBlob(image))));
      await api.sendChatMessageStream(conv.id, text, llm, undefined, {
        imageAssetIds: assets.map((asset) => asset.asset_id),
        onStage: (_s, label) => setStage(label),
        onWarning: (message) => setError(message),
        onToken: (piece) =>
          setMessages((m) => m.map((x) => (x.id === assistantId ? { ...x, content: x.content + piece } : x))),
        onDone: (payload) => {
          // 流式期间用户可能已切到别的会话：旧会话的完成事件只许更新列表，
          // 不许把界面强制切回旧会话、也不许用旧会话消息覆盖当前列表
          const stillViewing = currentIdRef.current === conversationId;
          if (stillViewing) {
            if (payload.conversation) setCurrent(payload.conversation);
            if (payload.messages?.length) setMessages(payload.messages);
          }
          setStreamingId(null);
          refreshList(false);
        },
        onError: (msg) => {
          setError(msg);
          setStreamingId(null);
          if (currentIdRef.current === conversationId) {
            void api.chatConversation(conversationId)
              .then((payload) => {
                if (currentIdRef.current === conversationId) setMessages(payload.messages);
              })
              .catch(() => {});
          }
        },
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      if (conversationId) {
        try {
          const payload = await api.chatConversation(conversationId);
          setMessages(payload.messages);
        } catch {}
      }
      setStreamingId(null);
    } finally {
      setLoading(false);
      setStage("");
    }
  }

  const starters = [
    "这家公司最值得追问的利润质量问题是什么？",
    "从估值、现金流、竞争格局给出下一步研究清单",
    "如果我要写深度报告，哪些证据最关键？",
  ];
  const readOnly = showArchived || !!current?.archived_at;
  const canSend = (!!input.trim() || pendingImages.length > 0) && !loading && !readOnly;

  return (
    <div className="h-full min-h-0 grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-4 p-5 overflow-hidden">
      <aside className="rounded-xl border border-border bg-card min-h-0 flex flex-col">
        <div className="p-3 border-b border-border space-y-3">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm">
              <MessageSquare className="size-4 text-gold" />
              对话
            </div>
            <button
              onClick={startConversation}
              className="grid place-items-center size-8 rounded-lg bg-secondary hover:bg-secondary/70 transition-colors"
              title="新对话"
            >
              <Plus className="size-4" />
            </button>
          </div>
          <div className="grid grid-cols-2 gap-1 rounded-lg bg-secondary/60 p-1 text-xs">
            <button
              onClick={() => setShowArchived(false)}
              className={`rounded-md px-2 py-1.5 transition-colors ${!showArchived ? "bg-card text-foreground" : "text-muted-foreground hover:text-foreground"}`}
            >
              进行中
            </button>
            <button
              onClick={() => setShowArchived(true)}
              className={`rounded-md px-2 py-1.5 transition-colors ${showArchived ? "bg-card text-foreground" : "text-muted-foreground hover:text-foreground"}`}
            >
              归档
            </button>
          </div>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1">
          {conversations.length === 0 && (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">
              {showArchived ? "暂无归档对话" : "暂无本地对话"}
            </div>
          )}
          {conversations.map((c) => (
            <div
              key={c.id}
              className={`group flex items-center gap-1 rounded-lg transition-colors ${
                current?.id === c.id ? "bg-accent text-gold" : "hover:bg-secondary/60"
              }`}
            >
              <button
                onClick={() => openConversation(c.id)}
                className="min-w-0 flex-1 px-3 py-2 text-left"
              >
                <div className="truncate text-sm">{c.title || "新对话"}</div>
                <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                  <span>{c.message_count == null ? "DeltaChannel" : `${c.message_count} 条`}</span>
                  <span>{c.archived_at ? "已归档" : "本地"}</span>
                </div>
              </button>
              <button
                onClick={() => setArchived(c, !c.archived_at)}
                className="grid place-items-center size-7 rounded-md text-muted-foreground opacity-70 hover:bg-card hover:text-foreground group-hover:opacity-100"
                title={c.archived_at ? "恢复" : "归档"}
              >
                {c.archived_at ? <ArchiveRestore className="size-3.5" /> : <Archive className="size-3.5" />}
              </button>
              <button
                onClick={() => deleteConversation(c)}
                className="mr-1 grid place-items-center size-7 rounded-md text-muted-foreground opacity-70 hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
                title="删除"
              >
                <Trash2 className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <section className="rounded-xl border border-border bg-card min-h-0 flex flex-col overflow-hidden">
        <div className="p-3 border-b border-border flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="truncate text-sm">{current?.title || (showArchived ? "归档对话" : "新对话")}</div>
            <div className="text-[10px] text-muted-foreground">DeltaChannel · 本地 SQLite</div>
          </div>
          <ModelPicker active={activeModel} onChange={setLlm} onGoSettings={onGoSettings} />
        </div>

        <div ref={scrollRef} onScroll={handleScroll} className="flex-1 min-h-0 overflow-y-auto p-5 space-y-4">
          {!messages.length && !loading && !showArchived && (
            <div className="h-full grid place-items-center">
              <div className="max-w-xl w-full">
                <div className="mx-auto mb-4 grid place-items-center size-12 rounded-xl bg-secondary">
                  <Bot className="size-6 text-gold" />
                </div>
                <div className="grid gap-2">
                  {starters.map((s) => (
                    <button
                      key={s}
                      onClick={() => setInput(s)}
                      className="rounded-lg border border-border bg-secondary/30 px-3 py-2 text-left text-sm text-foreground/85 hover:bg-secondary transition-colors"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {showArchived && !messages.length && (
            <div className="h-full grid place-items-center text-xs text-muted-foreground">
              从左侧选择一个归档对话查看历史。
            </div>
          )}

          {messages.map((m) => {
            const isStreaming = m.id === streamingId;
            const showThinking = isStreaming && !m.content;
            return (
              <div key={m.id} className={`flex gap-3 ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                {m.role === "assistant" && (
                  <div className="grid place-items-center size-8 rounded-lg bg-gold/15 text-gold shrink-0">
                    <Bot className="size-4" />
                  </div>
                )}
                <div className={`max-w-[78%] rounded-xl px-4 py-3 text-sm leading-relaxed ${
                  m.role === "user" ? "bg-gold text-[#17140a]" : "bg-secondary/50 text-foreground/90"
                }`}>
                  {m.role === "assistant" ? (
                    showThinking ? (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground py-1">
                        <Sparkles className="size-3.5 text-gold animate-pulse" />
                        <Typewriter text={stage || "正在思考"} className="text-foreground/80" />
                        <span className="inline-flex gap-0.5">
                          <span className="size-1 rounded-full bg-gold/70 animate-bounce" style={{ animationDelay: "0ms" }} />
                          <span className="size-1 rounded-full bg-gold/70 animate-bounce" style={{ animationDelay: "120ms" }} />
                          <span className="size-1 rounded-full bg-gold/70 animate-bounce" style={{ animationDelay: "240ms" }} />
                        </span>
                      </div>
                    ) : (
                      <>
                        <StructuredText content={m.content} />
                        {isStreaming && (
                          <span className="inline-block w-1.5 h-4 -mb-0.5 ml-0.5 bg-gold animate-pulse rounded-sm align-middle" />
                        )}
                      </>
                    )
                  ) : (
                    <>
                      {m.content && <span className="whitespace-pre-wrap">{m.content}</span>}
                      {(m.images?.length || m.image_asset_ids?.length) ? (
                        <div className={`grid grid-cols-2 gap-1.5 ${m.content ? "mt-2" : ""} max-w-[280px]`}>
                          {[...(m.images || []), ...(m.image_asset_ids || []).map(api.imageAssetUrl)].map((img, i) => (
                            <img
                              key={i}
                              src={img}
                              loading="lazy"
                              className="w-full max-h-32 object-cover rounded-lg border border-black/10 cursor-pointer hover:opacity-90 transition-opacity"
                              onClick={() => setLightbox(img)}
                            />
                          ))}
                        </div>
                      ) : null}
                    </>
                  )}
                </div>
                {m.role === "user" && (
                  <div className="grid place-items-center size-8 rounded-lg bg-secondary text-muted-foreground shrink-0">
                    <UserRound className="size-4" />
                  </div>
                )}
              </div>
            );
          })}

          {loading && !streamingId && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-4 animate-spin text-gold" />
              {stage || "正在准备"}
            </div>
          )}
        </div>

        {error && <div className="px-4 py-2 border-t border-border text-xs text-destructive">{error}</div>}
        <div className="p-3 border-t border-border">
          <div className="rounded-xl border border-border bg-input p-2">
            {/* 待发送图片预览条 */}
            {pendingImages.length > 0 && (
              <div className="flex flex-wrap gap-2 mb-2 pb-2 border-b border-border/50">
                {pendingImages.map((img, i) => (
                  <div key={i} className="relative group">
                    <img
                      src={img}
                      className="size-16 object-cover rounded-lg border border-border cursor-pointer hover:opacity-80 transition-opacity"
                      onClick={() => setLightbox(img)}
                    />
                    <button
                      onClick={() => setPendingImages((prev) => prev.filter((_, j) => j !== i))}
                      className="absolute -top-1.5 -right-1.5 grid place-items-center size-5 rounded-full bg-destructive text-destructive-foreground opacity-90 hover:opacity-100 shadow-sm"
                      title="移除图片"
                    >
                      <X className="size-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="flex items-end gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => {
                  if (e.target.files) void handleFiles(e.target.files);
                  e.target.value = "";
                }}
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={readOnly}
                className="grid place-items-center size-10 rounded-lg hover:bg-secondary text-muted-foreground hover:text-foreground disabled:opacity-50 transition-colors shrink-0"
                title="上传图片（可拖拽或粘贴）"
              >
                <ImageIcon className="size-4" />
              </button>
              <textarea
                value={input}
                disabled={readOnly}
                onChange={(e) => setInput(e.target.value)}
                onPaste={(e) => {
                  const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith("image/"));
                  if (files.length) {
                    e.preventDefault();
                    void handleFiles(files);
                  }
                }}
                onDrop={(e) => {
                  const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"));
                  if (files.length) {
                    e.preventDefault();
                    void handleFiles(files);
                  }
                }}
                onDragOver={(e) => e.preventDefault()}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
                  e.preventDefault();
                  void send();
                }}
                placeholder={readOnly ? "归档对话仅供查看，恢复后可继续提问" : "输入问题，可上传 K线/走势图等进行分析（支持粘贴/拖拽）"}
                rows={2}
                className="min-h-[44px] flex-1 resize-none bg-transparent px-2 py-1 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-60"
              />
              <button
                onClick={send}
                disabled={!canSend}
                className="grid place-items-center size-10 rounded-lg bg-gold text-[#17140a] hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                title="发送"
              >
                {loading ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* 图片全屏预览 */}
      {lightbox && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/80 backdrop-blur-sm cursor-zoom-out"
          onClick={() => setLightbox(null)}
        >
          <img
            src={lightbox}
            className="max-h-[90vh] max-w-[90vw] object-contain rounded-lg shadow-2xl"
            alt="预览"
          />
        </div>
      )}
    </div>
  );
}
