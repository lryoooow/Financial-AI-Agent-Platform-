import { useEffect, useState } from "react";
import { api, type McpServer, type PushChannel, type PushConfig, type SchedulerConfig, type WatchlistGroup } from "../../api/client";
import { useConfirm } from "../ui/confirm";
import { Skeleton } from "../ui/skeleton";

const field = "min-w-0 rounded border bg-input px-2 py-1.5";
const button = "rounded border px-2 py-1.5 hover:bg-accent disabled:opacity-50";
const primary = "rounded bg-action px-3 py-1.5 text-gold-foreground disabled:opacity-50";
const channels: [PushChannel, string][] = [["wework", "企业微信"], ["feishu", "飞书"], ["dingtalk", "钉钉"], ["smtp", "SMTP 邮件"]];
const smtpFields: [keyof PushConfig, string, boolean?][] = [
  ["host", "SMTP 主机"], ["sender", "发件人"], ["recipient", "收件人"], ["username", "用户名"], ["password", "SMTP 密码", true],
];

export function SchedulerSettings() {
  const confirm = useConfirm();
  const [config, setConfig] = useState<SchedulerConfig>();
  const [groups, setGroups] = useState<WatchlistGroup[]>([]);
  const [channel, setChannel] = useState<PushChannel>("wework");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api.schedulerConfig().then(setConfig).catch((cause) => setError(String(cause.message || cause)));
    api.watchlist().then(({ groups }) => setGroups(groups)).catch(() => {});
  }, []);

  async function act(operation: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setStatus("");
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  if (!config) return error ? <p role="alert" className="text-destructive">{error}</p> : <Skeleton className="h-48" />;
  const entry = config.channels[channel] || {};
  const patch = (next: Partial<PushConfig>) => setConfig({ ...config, channels: { ...config.channels, [channel]: { ...entry, ...next } } });
  return <section className="space-y-4">
    <h2 className="font-semibold">定时收盘巡检</h2>
    <p className="text-sm text-muted-foreground">应用运行期间按北京时间在 A 股交易日生成复盘和自选体检。桌面端可使用系统托盘保持运行。</p>
    <div className="grid gap-2 rounded border bg-card p-3 sm:grid-cols-2">
      <label className="flex items-center gap-2"><input type="checkbox" checked={config.enabled} onChange={(event) => setConfig({ ...config, enabled: event.target.checked })} /> 启用交易日巡检</label>
      <label className="flex items-center gap-2">执行时间 <input type="time" min="15:00" value={config.run_time} className={field} onChange={(event) => setConfig({ ...config, run_time: event.target.value })} /></label>
      <label className="flex items-center gap-2">巡检分组 <select className={field} value={config.group_id} onChange={(event) => setConfig({ ...config, group_id: event.target.value })}>
        <option value="">全部自选分组</option>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
      </select></label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={config.attach_pdf} onChange={(event) => setConfig({ ...config, attach_pdf: event.target.checked })} /> 邮件附上复盘 PDF</label>
    </div>
    <div className="space-y-3 rounded border bg-card p-3">
      <div className="flex flex-wrap gap-1.5">{channels.map(([value, label]) => <button type="button" key={value} aria-pressed={channel === value}
        className={button + " aria-pressed:bg-accent"} onClick={() => setChannel(value)}>{label}{config.channels[value]?.enabled ? " ✓" : ""}</button>)}</div>
      <label className="flex items-center gap-2"><input type="checkbox" checked={entry.enabled ?? false} onChange={(event) => patch({ enabled: event.target.checked })} /> 启用此渠道</label>
      {channel === "smtp" ? <div className="grid gap-2 sm:grid-cols-2">
        {smtpFields.map(([key, label, secret]) => <label className="grid gap-1" key={key}>{label}<input autoComplete="off" type={secret ? "password" : "text"} value={String(entry[key] ?? "")}
          className={field} onChange={(event) => patch({ [key]: event.target.value })} /></label>)}
        <label className="grid gap-1">端口<input type="number" value={entry.port ?? 465} min={1} max={65535} className={field} onChange={(event) => patch({ port: Number(event.target.value) })} /></label>
        <label className="grid gap-1">加密方式<select className={field} value={entry.tls ?? "ssl"} onChange={(event) => patch({ tls: event.target.value as "ssl" | "starttls" })}>
          <option value="ssl">TLS</option><option value="starttls">STARTTLS</option>
        </select></label>
      </div> : <div className="space-y-2">
        <label className="grid gap-1">机器人 Webhook<input type="password" autoComplete="off" className={field} value={entry.webhook ?? ""} onChange={(event) => patch({ webhook: event.target.value })} /></label>
        {channel !== "wework" && <label className="grid gap-1">签名密钥<input type="password" autoComplete="off" className={field} value={entry.secret ?? ""} onChange={(event) => patch({ secret: event.target.value })} /></label>}
      </div>}
    </div>
    <div className="flex flex-wrap gap-2">
      <button type="button" disabled={busy} className={primary} onClick={() => void act(async () => { setConfig(await api.saveScheduler(config)); setStatus("配置已保存。"); })}>保存巡检配置</button>
      <button type="button" disabled={busy || !entry.enabled} className={button} onClick={async () => {
        if (!await confirm("发送测试通知？", "测试将使用当前选中渠道已保存的配置。")) return;
        await act(async () => { const result = await api.testPush(channel); if (!result.ok) throw new Error(result.error); setStatus("测试通知已送达。"); });
      }}>发送测试通知</button>
    </div>
    {status && <p role="status" className="text-gold">{status}</p>}
    {error && <p role="alert" className="text-destructive">{error}</p>}
    <h3 className="font-medium">最近巡检</h3>
    {config.runs?.length ? <ul className="divide-y rounded border">
      {config.runs.map((run) => <li key={run.id} className="p-2 text-sm">
        {run.date} · {({ done: "已完成", partial: "部分成功", error: "失败", interrupted: "中断", running: "运行中" })[run.status] || run.status}
        {Object.entries(run.channels || {}).map(([id, result]) => " · " + (channels.find(([name]) => name === id)?.[1] || id) + (result.ok ? " 成功" : " 失败"))}
        {run.error && <p className="text-muted-foreground">{run.error}</p>}
      </li>)}
    </ul> : <p className="text-muted-foreground">尚无定时巡检记录。</p>}
  </section>;
}

type EditableMcp = Omit<McpServer, "command" | "env" | "allowed_tools"> & { command: string; env: string; allowed_tools: string };

export function McpSettings() {
  const [list, setList] = useState<EditableMcp[]>();
  const [editable, setEditable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  async function load() {
    const result = await api.mcpServers();
    setEditable(result.editable !== false);
    setList(result.servers.map((server) => ({ ...server, command: JSON.stringify(server.command),
      env: JSON.stringify(server.env), allowed_tools: server.allowed_tools.join(", ") })));
  }
  useEffect(() => { void load().catch((cause) => setError(String(cause.message || cause))); }, []);
  function patch(index: number, next: Partial<EditableMcp>) {
    setList((value) => value?.map((server, i) => i === index ? { ...server, ...next } : server));
  }
  async function save() {
    if (!list || busy) return;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      const servers = list.map((item) => ({ ...item, command: JSON.parse(item.command), env: JSON.parse(item.env),
        allowed_tools: item.allowed_tools.split(/[,，\s]+/).filter(Boolean) }));
      await api.saveMcpServers(servers);
      await load();
      setStatus("MCP 配置已保存。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }
  return <section className="space-y-3">
    <h2 className="font-semibold">MCP 工具服务</h2>
    <p className="text-sm text-muted-foreground">使用 stdio 连接已信任的本地工具。命令用 JSON 参数数组填写，工具名须加入白名单。</p>
    {!editable && <p className="text-sm text-muted-foreground">当前部署关闭了 stdio 工具执行。</p>}
    {!list && !error && <Skeleton className="h-36" />}
    {list?.map((item, index) => <fieldset key={index} disabled={!editable || busy} className="grid gap-2 rounded border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2">服务 ID<input className={field} value={item.id} onChange={(event) => patch(index, { id: event.target.value })} /></label>
        <label className="flex items-center gap-1"><input type="checkbox" checked={item.enabled} onChange={(event) => patch(index, { enabled: event.target.checked })} />启用</label>
        <button type="button" className={button + " ml-auto"} onClick={() => setList(list.filter((_, i) => index !== i))}>移除</button>
      </div>
      <label className="grid gap-1">命令参数<input className={field + " font-mono"} value={item.command} onChange={(event) => patch(index, { command: event.target.value })} /></label>
      <label className="grid gap-1">允许的工具<input className={field + " font-mono"} value={item.allowed_tools} onChange={(event) => patch(index, { allowed_tools: event.target.value })} /></label>
      <label className="grid gap-1">环境变量（JSON 对象）<input type="password" autoComplete="off" className={field + " font-mono"} value={item.env} onChange={(event) => patch(index, { env: event.target.value })} /></label>
    </fieldset>)}
    {editable && <div className="flex gap-2">
      <button type="button" className={button} disabled={busy || !list || list.length >= 12} onClick={() => setList([...(list || []),
        { id: "", command: '[""]', env: "{}", allowed_tools: "", enabled: false }])}>添加服务</button>
      <button type="button" className={primary} disabled={busy || !list} onClick={() => void save()}>保存 MCP 配置</button>
    </div>}
    {status && <p role="status" className="text-gold">{status}</p>}
    {error && <p role="alert" className="text-destructive">{error}</p>}
  </section>;
}
