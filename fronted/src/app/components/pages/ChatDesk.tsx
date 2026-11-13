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
  FlaskConical,
  Layers,
  Square,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { api, isReasoningModelLike, type ChatConversation, type ChatMessage, type LlmConfig, type ToolInvocation, type ToolArtifact, type HumanApproval } from "../../api/client";
import { ModelPicker } from "../ModelPicker";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { useConfirm } from "../ui/confirm";
import { isImeKeyEvent } from "../../lib/ime";
import { ChatReasoning } from "../chat/ChatReasoning";
import { ChatToolCalls } from "../chat/ChatToolCalls";
import { ArtifactDrawer } from "../chat/ArtifactDrawer";
import { StructuredText, toolCitations } from "../../lib/markdown";
import { ResearchCard } from "../research/ResearchCard";

/** 图片压缩：canvas 限宽 maxWidth=1280、限高 maxHeight=4096（长财报/长表格截图不压扁宽度导致字迹模糊）。 */
async function compressImage(file: File, maxWidth = 1280, quality = 0.84, maxHeight = 4096): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        let { width, height } = img;
        const ratio = Math.min(1, maxWidth / Math.max(1, width), maxHeight / Math.max(1, height));
        if (ratio < 1) {
          width = Math.max(1, Math.round(width * ratio));
          height = Math.max(1, Math.round(height * ratio));
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        if (!ctx) { reject(new Error("canvas 不可用")); return; }
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, width, height);
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
  onGoResearch,
  draftPrompt,
  onReportGenerated,
}: {
  activeModel: ActiveModel | null;
  onGoSettings: () => void;
  onGoResearch?: () => void;
  draftPrompt?: { text: string; nonce: number } | null;
  onReportGenerated?: (r: { id: string; name: string; code: string; template: string; date: string; summary: string }) => void;
}) {
  const confirm = useConfirm();
  const [artifact, setArtifact] = useState<ToolArtifact | null>(null);
  const [chatArtifacts, setChatArtifacts] = useState<ToolArtifact[]>([]);
  const [handledApprovals, setHandledApprovals] = useState<Set<string>>(() => new Set());
  const [editedSymbol, setEditedSymbol] = useState("");
  const [editingApproval, setEditingApproval] = useState("");
  const [notice, setNotice] = useState("");
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
  const [chatTools, setChatTools] = useState<ToolInvocation[]>([]);
  // v1.7.0：思维链瞬态展示（reasoning SSE）——不进 messages、不落库，流结束即清。
  const [reasoning, setReasoning] = useState("");          // CoT 文本（尾部 6000 字截断，防长 CoT 撑爆渲染）
  const [thinkSecs, setThinkSecs] = useState(0);           // 已等待秒数
  const [reasoningLive, setReasoningLive] = useState(false); // CoT 仍在更新（多轮工具调用间复现面板）
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const sendingRef = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const activeConvIdRef = useRef<string | null>(null);
  const openSeqRef = useRef(0);
  const stopRef = useRef<AbortController | null>(null);

  useEffect(() => {
    activeConvIdRef.current = current?.id ?? null;
    setArtifact(null);
  }, [current?.id]);

  const handledDraftNonceRef = useRef(0);
  useEffect(() => {
    if (!draftPrompt || handledDraftNonceRef.current === draftPrompt.nonce) return;
    handledDraftNonceRef.current = draftPrompt.nonce;
    if (showArchived) setShowArchived(false);
    setInput(draftPrompt.text);
    window.setTimeout(() => {
      const el = inputRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
      }
    }, 60);
  }, [draftPrompt, showArchived]);

  // 图片上传状态
  const [pendingImages, setPendingImages] = useState<string[]>([]); // 待发送的 base64 data URL
  const [lightbox, setLightbox] = useState<string | null>(null);   // 全屏预览的图片 URL
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!lightbox) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !isImeKeyEvent(e)) setLightbox(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox]);

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

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickToBottomRef.current) return;
    if (streamingId) {
      el.scrollTop = el.scrollHeight;
    } else {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, [messages, loading, streamingId]);

  async function openConversation(id: string) {
    setError("");
    stickToBottomRef.current = true;
    // v1.6.0（审计 C1）：请求序号守卫 —— 快速切换会话时，旧请求的返回不得覆盖新会话
    const seq = ++openSeqRef.current;
    try {
      const r = await api.chatConversation(id);
      if (openSeqRef.current !== seq) return;
      setCurrent(r.conversation);
      setMessages(r.messages);
    } catch (e) {
      if (openSeqRef.current !== seq) return;
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
    if (!await confirm("删除对话", "将删除「" + (c.title || "新对话") + "」及其本地历史。")) return;
    setError("");
    // v1.6.0（审计 C1）：删除正在流式的对话先停流，避免 done 回调把已删会话写回状态
    if (current?.id === c.id) stopRef.current?.abort();
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
    if (sendingRef.current) return;
    const text = input.trim();
    const imgs = [...pendingImages];
    if ((!text && !imgs.length) || loading || showArchived || current?.archived_at) return;
    sendingRef.current = true;
    stickToBottomRef.current = true;
    setInput("");
    setPendingImages([]);
    setError("");
    setLoading(true);
    setStage("正在理解问题");
    setChatTools([]);
    setChatArtifacts([]);
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
    // v1.7.0：思维链面板三态复位（新一轮发送）
    setReasoning("");
    setThinkSecs(0);
    setReasoningLive(false);
    // v1.6.3（D2）：真「无输出」看门狗（活动刷新），且仅在本会话前台可见时才计时 ——
    // 页面隐藏/用户切到其他会话时暂停计时（后台流不应被误杀），回前台重置基准。
    // v1.6.5：超时按模型档位缩放。思考型模型（显式档位或名字命中推理模式）在思维链
    // 期间 agent 流无任何事件（stage/token 都在模型调用边界才发），90s 会把合法的
    // 深度思考流拦腰杀掉。普通模型 120s 与后端读超时对齐（真挂死时后端报错先到），
    // 思考模型 330s 略高于后端 300s 读超时，只兜完全静默的死连接。
    // 已知边界：llm 为 null（仅环境变量配模型、Settings 未配的部署）时按普通档
    // 120s 处理——该形态前端无从感知模型名，可接受（1.6.4 前统一 90s，仍是改善）。
    const deepThink = !!llm?.reasoning_effort || isReasoningModelLike(llm?.model || "");
    const watchdogMs = deepThink ? 330_000 : 120_000;
    const ac = new AbortController();
    stopRef.current = ac;
    let lastAct = Date.now();
    let convId = ""; // send 域内声明，catch 的 stale 判定用它（闭包 current 会过期）
    const isStale = () => convId !== "" && activeConvIdRef.current !== convId;
    const watchdog = window.setInterval(() => {
      if (document.hidden || isStale()) return; // 后台/切走：暂停看门狗
      if (Date.now() - lastAct > watchdogMs) {
        window.clearInterval(watchdog);
        ac.abort(new Error("模型长时间无输出"));
      }
    }, 5_000);
    const onVisible = () => { if (!document.hidden) lastAct = Date.now(); };
    document.addEventListener("visibilitychange", onVisible);
    // v1.7.0：已等待秒数计时（一旦正文开始流出且不在新一轮 CoT 中则暂停累加，与后端 TurnTranscript 口径一致）
    let thinkingPaused = false;
    let latestTools: ToolInvocation[] = [];
    let latestArtifacts: ToolArtifact[] = [];
    let latestReasoning = "";
    let latestThinkSecs = 0;
    const thinkTimer = window.setInterval(() => {
      if (thinkingPaused) return;
      latestThinkSecs += 1;
      setThinkSecs(latestThinkSecs);
    }, 1_000);
    let streamOpened = false;
    try {
      let conv = current;
      if (!conv) {
        const created = await api.createChatConversation(text.slice(0, 36) || "图片对话", llm);
        conv = created.conversation;
        setCurrent(conv);
        setConversations((prev) => [conv!, ...prev]);
      }
      convId = conv.id;
      await api.sendChatMessageStream(convId, text, llm, imgs.length ? imgs : undefined, {
        signal: ac.signal,
        onStage: (_s, label, heartbeat) => {
          streamOpened = true;
          if (isStale()) return;
          // 后端 15s 合成保活帧仅用于保活代理/连接，不刷新看门狗 lastAct（否则上游模型真挂死时看门狗永不触发）
          if (!heartbeat) lastAct = Date.now();
          setStage(label);
        },
        onToolCall: (ev) => {
          streamOpened = true;
          if (isStale()) return;
          lastAct = Date.now();
          thinkingPaused = false;
          setChatTools((previous) => {
            const match = previous.findIndex((call) =>
              ev.call_id ? call.call_id === ev.call_id : call.name === ev.name && call.status === "start"
            );
            const next = match < 0 ? [...previous, ev] : previous.map((call, index) => index === match ? { ...call, ...ev } : call);
            latestTools = next;
            return next;
          });
        },
        onArtifact: (event) => {
          streamOpened = true;
          if (!isStale()) {
            setChatArtifacts((current) => {
              const next = [...current, event];
              latestArtifacts = next;
              return next;
            });
          }
        },
        onReasoning: (piece) => {
          streamOpened = true;
          if (isStale()) return;
          // 思维链活跃 = 真实的看门狗续命信号（比 stage 心跳更强的证据），且面板置 live
          lastAct = Date.now();
          thinkingPaused = false;
          setReasoningLive(true);
          setReasoning((r) => {
            const next = (r + piece).slice(-6000);
            latestReasoning = next;
            return next;
          });
        },
        onCompacted: (info) => {
          streamOpened = true;
          if (isStale()) return;
          lastAct = Date.now();
          setNotice(`较早的对话已自动压缩为要点摘要（第 ${info.generation} 次），后续回答基于摘要与最近几轮继续。`);
        },
        onToken: (piece) => {
          streamOpened = true;
          if (isStale()) return;
          lastAct = Date.now();
          thinkingPaused = true;
          setReasoningLive(false); // 正文开始流：折叠思考面板（下一轮工具调用的 CoT 会再点亮）
          setMessages((m) => m.map((x) => (x.id === assistantId ? { ...x, content: x.content + piece } : x)));
        },
        onDone: (payload) => {
          streamOpened = true;
          refreshList(false).catch(() => {});
          if (isStale()) return; // 用户已切走：旧流的完成态不得覆盖当前会话
          if (payload.conversation) setCurrent(payload.conversation);
          setMessages(payload.messages?.length ? payload.messages : (m) => m);
          setStreamingId(null);
        },
        onError: (msg) => {
          streamOpened = true;
          if (isStale()) return;
          setError(msg);
          setMessages((m) => m.filter((x) => !(x.id === assistantId && !x.content && !latestTools.length)));
          setStreamingId(null);
        },
      });
    } catch (e) {
      // 会话已删/已切走时不落无关的错误横幅（用 send 域 convId 判定，闭包 current 已过期）
      const stale = isStale();
      const isAbort = (e as DOMException)?.name === "AbortError" || (e as Error)?.message === "模型长时间无输出";
      if (isAbort) {
        // 用户停止/看门狗：已生成部分及已执行的工具轨迹保留在当前气泡上
        if (!stale) setError("已停止生成");
      } else if (!stale) {
        setError(e instanceof Error ? e.message : String(e));
      }
      if (!streamOpened && !isAbort) {
        // 流尚未建立即失败（如网络断连 / 409 / 422）：后端尚未落库提问，自动恢复输入框与待发图片，不丢用户输入
        setMessages((m) => m.filter((x) => x.id !== optimistic.id && !(x.id === assistantId && !x.content)));
        if (!stale) {
          setInput((cur) => cur || text);
          if (imgs.length) setPendingImages((cur) => (cur.length ? cur : imgs));
        }
      } else {
        // 后端在流开始时已持久化用户消息：若有部分正文或工具轨迹则挂回气泡，否则移除空占位
        setMessages((m) =>
          m
            .map((x) =>
              x.id === assistantId && (x.content || latestTools.length)
                ? {
                    ...x,
                    tool_invocations: latestTools.length ? latestTools.map((t) => (t.status === "start" ? { ...t, status: "aborted" as const } : t)) : x.tool_invocations,
                    artifacts: latestArtifacts.length ? latestArtifacts : x.artifacts,
                    reasoning_summary: latestReasoning || x.reasoning_summary,
                    thinking_elapsed_ms: latestThinkSecs * 1000 || x.thinking_elapsed_ms,
                  }
                : x,
            )
            .filter((x) => !(x.id === assistantId && !x.content && !latestTools.length)),
        );
      }
      setStreamingId(null);
    } finally {
      window.clearInterval(watchdog);
      window.clearInterval(thinkTimer);
      document.removeEventListener("visibilitychange", onVisible);
      if (stopRef.current === ac) stopRef.current = null;
      sendingRef.current = false;
      setLoading(false);
      setStage("");
      setChatTools([]);
      setChatArtifacts([]);
      // 思维链面板随流结束清空（done/error/中止/看门狗统一走这里）
      setReasoning("");
      setThinkSecs(0);
      setReasoningLive(false);
    }
  }

  async function resume(approval: HumanApproval, action: "approve" | "edit" | "reject", symbol: string) {
    if (!current || sendingRef.current) return;
    const conversationId = current.id;
    const isStale = () => activeConvIdRef.current !== conversationId;
    const controller = new AbortController();
    stopRef.current = controller;
    sendingRef.current = true;
    stickToBottomRef.current = true;
    setLoading(true);
    setError("");
    setChatTools([]);
    setChatArtifacts([]);
    setReasoning("");
    setThinkSecs(0);
    setReasoningLive(false);

    const markApprovalStatus = (status: "approved" | "rejected") => {
      setHandledApprovals((prior) => new Set(prior).add(approval.checkpoint_id));
      setMessages((prev) =>
        prev.map((m) =>
          m.approval?.checkpoint_id === approval.checkpoint_id
            ? { ...m, approval: { ...m.approval, status } }
            : m,
        ),
      );
    };

    if (approval.kind === "batch_research") {
      try {
        const result = await api.approveBatch(conversationId, approval.checkpoint_id, action, symbol);
        markApprovalStatus(action === "reject" ? "rejected" : "approved");
        setNotice(action === "reject" ? "已取消批量研究。" : "已排入 " + (result.task_ids?.length || 0) + " 项深度研究。");
        setEditingApproval("");
        setEditedSymbol("");
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        if (stopRef.current === controller) stopRef.current = null;
        sendingRef.current = false;
        setLoading(false);
      }
      return;
    }

    const assistantId = action === "reject" ? "" : `resume-${Date.now()}`;
    if (assistantId) {
      setStage("正在核验标的并继续分析");
      setMessages((m) => [
        ...m.map((item) =>
          item.approval?.checkpoint_id === approval.checkpoint_id
            ? { ...item, approval: { ...item.approval, status: "approved" as const } }
            : item,
        ),
        { id: assistantId, role: "assistant", content: "", created_at: new Date().toISOString() },
      ]);
      setStreamingId(assistantId);
    }

    let thinkingPaused = false;
    let latestTools: ToolInvocation[] = [];
    let latestArtifacts: ToolArtifact[] = [];
    let latestReasoning = "";
    let latestThinkSecs = 0;
    const thinkTimer = assistantId
      ? window.setInterval(() => {
          if (thinkingPaused) return;
          latestThinkSecs += 1;
          setThinkSecs(latestThinkSecs);
        }, 1_000)
      : null;

    try {
      await api.resumeChat(conversationId, approval.checkpoint_id, action, symbol, {
        signal: controller.signal,
        onStage: (_stage, label) => {
          if (!isStale()) setStage(label);
        },
        onToolCall: (ev) => {
          if (isStale()) return;
          thinkingPaused = false;
          setChatTools((previous) => {
            const match = previous.findIndex((call) =>
              ev.call_id ? call.call_id === ev.call_id : call.name === ev.name && call.status === "start"
            );
            const next = match < 0 ? [...previous, ev] : previous.map((call, index) => index === match ? { ...call, ...ev } : call);
            latestTools = next;
            return next;
          });
        },
        onArtifact: (event) => {
          if (!isStale()) {
            setChatArtifacts((cur) => {
              const next = [...cur, event];
              latestArtifacts = next;
              return next;
            });
          }
        },
        onReasoning: (piece) => {
          if (isStale()) return;
          thinkingPaused = false;
          setReasoningLive(true);
          setReasoning((r) => {
            const next = (r + piece).slice(-6000);
            latestReasoning = next;
            return next;
          });
        },
        onCompacted: (info) => {
          if (!isStale()) {
            setNotice(`较早的对话已自动压缩为要点摘要（第 ${info.generation} 次），后续回答基于摘要与最近几轮继续。`);
          }
        },
        onToken: (piece) => {
          if (isStale() || !assistantId) return;
          thinkingPaused = true;
          setReasoningLive(false);
          setMessages((m) => m.map((x) => (x.id === assistantId ? { ...x, content: x.content + piece } : x)));
        },
        onDone: (payload) => {
          refreshList(false).catch(() => {});
          if (isStale()) return;
          if (payload.conversation) setCurrent(payload.conversation);
          if (payload.messages?.length) setMessages(payload.messages);
          else if (action === "reject") markApprovalStatus("rejected");
          setStreamingId(null);
        },
        onError: (msg) => {
          if (isStale()) return;
          setError(msg);
          if (assistantId) {
            setMessages((m) => m.filter((x) => !(x.id === assistantId && !x.content && !latestTools.length)));
          }
          setStreamingId(null);
        },
      });
      markApprovalStatus(action === "reject" ? "rejected" : "approved");
      if (action === "reject" && !isStale()) {
        api.chatConversation(conversationId)
          .then((r) => {
            if (!isStale()) {
              setCurrent(r.conversation);
              setMessages(r.messages);
            }
          })
          .catch(() => {});
      }
      setEditingApproval("");
      setEditedSymbol("");
    } catch (cause) {
      const isAbort = controller.signal.aborted || (cause as DOMException)?.name === "AbortError";
      if (!isAbort && !isStale()) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } else if (isAbort && !isStale()) {
        setError("已停止生成");
      }
      if (assistantId) {
        setMessages((m) =>
          m
            .map((x) =>
              x.id === assistantId && (x.content || latestTools.length)
                ? {
                    ...x,
                    tool_invocations: latestTools.length ? latestTools.map((t) => (t.status === "start" ? { ...t, status: "aborted" as const } : t)) : x.tool_invocations,
                    artifacts: latestArtifacts.length ? latestArtifacts : x.artifacts,
                    reasoning_summary: latestReasoning || x.reasoning_summary,
                    thinking_elapsed_ms: latestThinkSecs * 1000 || x.thinking_elapsed_ms,
                  }
                : x,
            )
            .filter((x) => !(x.id === assistantId && !x.content && !latestTools.length)),
        );
      }
      setStreamingId(null);
    } finally {
      if (thinkTimer !== null) window.clearInterval(thinkTimer);
      if (stopRef.current === controller) stopRef.current = null;
      sendingRef.current = false;
      setLoading(false);
      setStage("");
      setChatTools([]);
      setChatArtifacts([]);
      setReasoning("");
      setThinkSecs(0);
      setReasoningLive(false);
    }
  }

  const starters = [
    "这家公司最值得追问的利润质量问题是什么？",
    "从估值、现金流、竞争格局给出下一步研究清单",
    "我之前研究过的标的里，哪些估值处于低位？",
  ];
  const readOnly = showArchived || !!current?.archived_at;
  const canSend = (!!input.trim() || pendingImages.length > 0) && !loading && !readOnly;

  return (
    <div className="h-full min-h-0 grid grid-cols-[minmax(150px,215px)_minmax(0,1fr)] overflow-hidden">
      <aside className="border-r bg-card min-h-0 flex flex-col">
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

      <PanelGroup direction="horizontal" className="min-w-0 min-h-0">
      <Panel minSize={45}>
      <section className="h-full min-h-0 flex flex-col overflow-hidden bg-background">
        <div className="p-3 border-b border-border flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="truncate text-sm">{current?.title || (showArchived ? "归档对话" : "投研问答")}</div>
            <div className="text-[10px] text-muted-foreground">多轮问股 · 看图解盘 · 本地知识库联动</div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {onGoResearch && (
              <button
                type="button"
                onClick={onGoResearch}
                className="flex items-center gap-1.5 rounded-lg border border-gold/35 bg-gold/10 hover:bg-gold/20 px-2.5 py-1.5 text-xs text-gold transition-colors"
                title="切换到独立「深度研究」工作台一键生成完整研报"
              >
                <FlaskConical className="size-3.5" /> 深度研究台
              </button>
            )}
            <ModelPicker active={activeModel} onChange={setLlm} onGoSettings={onGoSettings} />
          </div>
        </div>

        <div
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
          }}
          className="flex-1 min-h-0 overflow-y-auto p-5 space-y-4"
        >
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
                  {onGoResearch && (
                    <button
                      type="button"
                      onClick={onGoResearch}
                      className="mt-1 flex items-center justify-between rounded-lg border border-gold/35 bg-gold/[0.08] px-3.5 py-2.5 text-left text-xs text-gold hover:bg-gold/[0.14] transition-colors"
                    >
                      <span className="flex items-center gap-2">
                        <FlaskConical className="size-4 shrink-0" />
                        <span>需要生成完整的个股 / 行业 / 财报 / 风险研报？前往独立「深度研究」工作台</span>
                      </span>
                      <span className="font-medium shrink-0">去发起 →</span>
                    </button>
                  )}
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
            if (m.kind === "research" && m.research) {
              return (
                <div key={m.id} className="flex gap-3">
                  <div className="grid place-items-center size-8 rounded-lg bg-gold/15 text-gold shrink-0">
                    <FlaskConical className="size-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <ResearchCard
                      research={m.research}
                      initialStatus={m.research.status}
                      initialReportId={m.research.report_id}
                      initialError={m.research.error}
                      onDone={onReportGenerated}
                    />
                  </div>
                </div>
              );
            }
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
                  m.role === "user" ? "bg-action text-gold-foreground" : "bg-secondary/50 text-foreground/90"
                }`}>
                  {m.role === "assistant" ? (
                    <>
                      {(isStreaming ? reasoning : m.reasoning_summary) && <ChatReasoning
                        text={(isStreaming ? reasoning : m.reasoning_summary) || ""}
                        elapsedMs={isStreaming ? thinkSecs * 1000 : m.thinking_elapsed_ms || 0}
                        live={isStreaming && reasoningLive}
                      />}
                      {!!(isStreaming ? chatTools : m.tool_invocations)?.length && <ChatToolCalls
                        calls={(isStreaming ? chatTools : m.tool_invocations) || []}
                        artifacts={(isStreaming ? chatArtifacts : m.artifacts) || []}
                        onOpen={setArtifact}
                      />}
                      {m.approval && (!m.approval.status || m.approval.status === "pending") && !handledApprovals.has(m.approval.checkpoint_id) && !readOnly && <div className="my-2 flex flex-wrap gap-2">
                        {m.approval.kind === "batch_research" ? <>
                          <p className="w-full text-xs text-muted-foreground">{m.approval.candidates.map((candidate) => candidate.name).join("、")}</p>
                          <button type="button" disabled={loading} className="rounded bg-action px-2 py-1 text-gold-foreground"
                            onClick={() => void resume(m.approval!, "approve", "")}>提交全部</button>
                        </> : m.approval.candidates.map((candidate) => <button key={candidate.code} type="button" disabled={loading}
                          className="rounded border px-2 py-1 hover:bg-accent" onClick={() => void resume(m.approval!, "approve", candidate.code)}>
                          {candidate.name} <span className="font-mono text-xs">{candidate.code}</span>
                        </button>)}
                        {editingApproval === m.approval.checkpoint_id ? <>
                          <input aria-label="标的代码" value={editedSymbol} onChange={(e) => setEditedSymbol(e.target.value)}
                            onKeyDown={(e) => { if (e.key === "Enter" && !isImeKeyEvent(e.nativeEvent) && editedSymbol.trim()) void resume(m.approval!, "edit", editedSymbol); }}
                            className="w-48 rounded border bg-input px-2" placeholder={m.approval.kind === "batch_research" ? "以逗号分隔原批次代码" : "标的代码"} />
                          <button type="button" disabled={loading || !editedSymbol.trim()} className="rounded border px-2 py-1"
                            onClick={() => void resume(m.approval!, "edit", editedSymbol)}>确认代码</button>
                        </> : <button type="button" disabled={loading} className="rounded border px-2 py-1"
                          onClick={() => { setEditingApproval(m.approval!.checkpoint_id); setEditedSymbol(""); }}>修改</button>}
                        <button type="button" disabled={loading} className="rounded border px-2 py-1" onClick={() => void resume(m.approval!, "reject", "")}>取消</button>
                      </div>}
                      {showThinking ? (
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
                        <StructuredText content={m.content} streaming={isStreaming} citations={toolCitations(isStreaming ? chatArtifacts : m.artifacts)} />
                        {isStreaming && (
                          <span className="inline-block w-1.5 h-4 -mb-0.5 ml-0.5 bg-gold animate-pulse rounded-sm align-middle" />
                        )}
                      </>
                    )}
                    </>
                  ) : (
                    <>
                      {m.kind === "research_request" && (
                        <span className="mr-1.5 inline-flex items-center gap-1 rounded bg-black/10 px-1.5 py-0.5 text-[10px] align-middle">
                          <FlaskConical className="size-3" /> 深度研究
                        </span>
                      )}
                      {m.content && <span className="whitespace-pre-wrap">{m.kind === "research_request" ? m.content.replace(/^深度研究：/, "") : m.content}</span>}
                      {m.images?.length ? (
                        <div className={`grid grid-cols-2 gap-1.5 ${m.content ? "mt-2" : ""} max-w-[280px]`}>
                          {m.images.map((img, i) => (
                            <img
                              key={i}
                              src={img}
                              loading="lazy"
                              alt="上传附图"
                              onError={(e) => {
                                e.currentTarget.style.display = "none";
                              }}
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

        {notice && (
          <div className="px-4 py-1.5 border-t border-border text-[11px] text-muted-foreground flex items-center gap-2">
            <Layers className="size-3.5 text-gold shrink-0" />
            <span className="flex-1">{notice}</span>
            <button onClick={() => setNotice("")} className="hover:text-foreground"><X className="size-3" /></button>
          </div>
        )}
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
                ref={inputRef}
                value={input}
                disabled={readOnly}
                onChange={(e) => setInput(e.target.value)}
                onPaste={(e) => {
                  if (readOnly) return;
                  const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith("image/"));
                  if (files.length) {
                    e.preventDefault();
                    void handleFiles(files);
                  }
                }}
                onDrop={(e) => {
                  if (readOnly) return;
                  const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"));
                  if (files.length) {
                    e.preventDefault();
                    void handleFiles(files);
                  }
                }}
                onDragOver={(e) => e.preventDefault()}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" || e.shiftKey || isImeKeyEvent(e.nativeEvent)) return;
                  e.preventDefault();
                  void send();
                }}
                placeholder={readOnly
                  ? "归档对话仅供查看，恢复后可继续提问"
                  : "输入投研问题，可上传 K 线/财报截图分析（支持粘贴/拖拽），也可追问已生成的深度研报…"}
                rows={2}
                className="min-h-[44px] flex-1 resize-none bg-transparent px-2 py-1 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-60"
              />
              {loading && (
                <button
                  onClick={() => stopRef.current?.abort()}
                  className="grid place-items-center size-10 rounded-lg bg-destructive/15 text-destructive hover:bg-destructive/25 transition-colors shrink-0"
                  title="停止生成"
                >
                  <Square className="size-3.5 fill-current" />
                </button>
              )}
              <button
                onClick={send}
                disabled={!canSend}
                className="grid place-items-center size-10 rounded-lg bg-action text-gold-foreground hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                title="发送"
              >
                {loading ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
              </button>
            </div>
          </div>
        </div>
      </section>
      </Panel>
      {artifact && <>
        <PanelResizeHandle className="w-1 bg-border hover:bg-action" />
        <Panel defaultSize={34} minSize={23} maxSize={55}>
          <ArtifactDrawer entry={artifact} onClose={() => setArtifact(null)} />
        </Panel>
      </>}
      </PanelGroup>

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
