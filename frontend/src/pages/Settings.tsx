import { useEffect, useState } from "react";
import { KeyRound, Sparkles, ShieldCheck, Check, Trash2, Terminal, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { toast } from "sonner";
import { loadLlm, saveLlm, clearLlm, staleBlockedProvider } from "@/lib/llm";
import { loadAccessKey, saveAccessKey, authHeaders } from "@/lib/api";
import { subscriptionModels, apiModels, PROVIDER_BASE, isCliProvider, aiModels, cliKindOf,
  primeCliAvailability, cliAvailability, cliAvailState, serverAllowsCli,
  type CliAvailability, type CliAvailState, type ProviderId } from "@/lib/ai-models";

export function Settings() {
  const existing = loadLlm();
  const existingIsCli = existing ? isCliProvider(existing.provider) : false;
  const [staleBlocked, setStaleBlocked] = useState<string | null>(staleBlockedProvider());

  const [mode, setMode] = useState<"api" | "subscription">(
    (existing && existingIsCli) || staleBlocked ? "subscription" : "api");
  // 订阅：选中的 CLI model id
  const [cliId, setCliId] = useState(existing && existingIsCli ? existing.model : "");
  // API：选中的模型 id + 可编辑的 baseURL / model / key
  const firstApi = apiModels[0];
  // ponytail: custom model not in preset list → select won't match any option → fallback to first item
  const matchedApi = existing && !existingIsCli ? apiModels.find((m) => m.id === existing.model) : null;
  const [apiId, setApiId] = useState(matchedApi ? matchedApi.id : "custom");
  const [baseURL, setBaseURL] = useState(existing && !existingIsCli ? existing.baseURL : (PROVIDER_BASE[firstApi.provider] || ""));
  const [modelName, setModelName] = useState(existing && !existingIsCli ? existing.model : firstApi.id);
  const [apiKey, setApiKey] = useState(existing && !existingIsCli ? existing.apiKey : "");
  // 后端访问密钥（对应部署时的 VR_API_KEY）；本机自用不设鉴权时留空
  const [accessKey, setAccessKey] = useState(loadAccessKey());

  const [cliAvail, setCliAvail] = useState<CliAvailability | null>(cliAvailability());
  const [availState, setAvailState] = useState<CliAvailState>(cliAvailState());
  
  const refreshAvail = () =>
    primeCliAvailability(authHeaders()).then((d) => {
      setCliAvail(d);
      setAvailState(cliAvailState());
      setStaleBlocked(staleBlockedProvider());
    });

  useEffect(() => {
    void refreshAvail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  
  // 卡片上那句"支持哪些 CLI"，直接由服务端上报的 allowed 列表生成
  const allowedCliLabel = (() => {
    if (availState !== "ready") return "以后端上报为准";
    const ok = subscriptionModels.filter((m) => serverAllowsCli(m.provider) === true);
    return ok.length ? ok.map((m) => m.name).join(" / ") : "当前后端未放行任何 CLI";
  })();

  const cliState = (m: (typeof subscriptionModels)[number]): { ok: boolean; why: string | null } => {
    if (m.comingSoon) return { ok: false, why: "开发中" };
    if (availState === "loading" || availState === "idle") return { ok: false, why: "正在确认本机可用的 CLI…" };
    if (availState === "failed") return { ok: false, why: "无法向后端确认可用性" };
    const st = cliAvail?.clis.find((c) => c.kind === cliKindOf(m.provider));
    if (!st) return { ok: false, why: "后端不认识这个 CLI" };
    if (!st.allowed) return { ok: false, why: m.blocked ?? st.reason ?? "已禁用" };
    if (!st.installed) return { ok: false, why: "本机未安装这个命令" };
    return { ok: true, why: null };
  };

  const providerOf = (id: string): ProviderId => aiModels.find((m) => m.id === id)?.provider ?? "openai-compatible";

  const pickApiModel = (id: string) => {
    const m = apiModels.find((x) => x.id === id);
    if (!m) return;
    setApiId(id);
    setModelName(id);
    setBaseURL(PROVIDER_BASE[m.provider] || "");
  };

  const saveApi = () => {
    if (!baseURL.trim() || !apiKey.trim() || !modelName.trim()) {
      toast.error("请填完 Base URL、API Key、Model");
      return;
    }
    saveLlm({ provider: providerOf(apiId), baseURL: baseURL.trim(), apiKey: apiKey.trim(), model: modelName.trim() });
    setStaleBlocked(null);   // 配置已换新 → 那条"原配置失效"的提示要收起来
    toast.success("已保存到本地，全站「问 AI / 复盘」现在可用");
  };

  const saveSubscription = () => {
    const m = subscriptionModels.find((x) => x.id === cliId);
    if (!m || m.comingSoon) {
      toast.error("请选择一个可用的订阅（暂不支持标「即将支持」的）");
      return;
    }
    const st = cliState(m);
    if (!st.ok) {
      toast.error(`「${m.name}」不可用：${st.why ?? "未知原因"}`);
      return;
    }
    saveLlm({ provider: m.provider, baseURL: "", apiKey: "", model: m.id });
    setStaleBlocked(null);
    toast.success(`已选「${m.name}」订阅，全站「问 AI / 复盘」将调用本机 ${m.name}`);
  };

  const forget = () => {
    clearLlm();
    setApiKey("");
    setCliId("");
    setStaleBlocked(null);   // 旧配置已被清掉，提示没有对象了
    toast.success("已清除本地配置");
  };

  const saveAccess = () => {
    const k = accessKey.trim();
    saveAccessKey(k);
    setAccessKey(k);
    toast.success(k ? "已保存后端访问密钥（存本地）" : "已清除后端访问密钥");
    void refreshAvail();
  };

  return (
    <div>
      <PageHeader title="接入 AI" subtitle="配置一次，全站的「问 AI」「复盘」都能用你自己的模型" />

      <div className="mb-4 flex items-start gap-2 rounded-lg border border-success/25 bg-success/5 p-3 text-xs text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-success" />
        <span>API key <b className="text-foreground">只存在你本地浏览器</b>，仅在你提问时发给你自己的后端去调模型，不上传、不进仓库。所有分析由你的模型给出，本产品不校准。</span>
      </div>

      {/* 两种接入方式 */}
      {staleBlocked && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <span>
            你之前选的那个 AI CLI <b className="text-foreground">已被禁用</b>（{staleBlocked}），原配置已自动失效，
            现在「问 AI」不可用。请在下面改选 <b className="text-foreground">Claude Code</b>（订阅接入）或填一个 API key。
          </span>
        </div>
      )}
      <div className="mb-4 grid gap-3 sm:grid-cols-2">
        <GlassCard glow={mode === "subscription"} onClick={() => setMode("subscription")}
          className={mode === "subscription" ? "ring-1 ring-primary/40" : "opacity-80"}>
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            <h3 className="font-semibold">订阅接入</h3>
            {mode === "subscription" && <Check className="ml-auto h-4 w-4 text-primary" />}
          </div>
          {}
          <p className="mt-1 text-xs text-muted-foreground">调本机已登录的 AI CLI（{allowedCliLabel}），用订阅额度，<b className="text-foreground">免 API key</b>。需后端在本机跑。</p>
        </GlassCard>

        <GlassCard glow={mode === "api"} onClick={() => setMode("api")}
          className={mode === "api" ? "ring-1 ring-primary/40" : "opacity-80"}>
          <div className="flex items-center gap-2">
            <KeyRound className="h-5 w-5 text-primary" />
            <h3 className="font-semibold">API 接入</h3>
            {mode === "api" && <Check className="ml-auto h-4 w-4 text-primary" />}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">粘贴 API key，支持 DeepSeek / 豆包 / MiniMax / OpenAI / OpenRouter / 任意兼容端点。<b className="text-foreground">现已可用。</b></p>
        </GlassCard>
      </div>

      <GlassCard>
        {mode === "subscription" ? (
          <div className="space-y-3 text-sm">
            <p className="text-xs text-muted-foreground">
              选一个你本机已安装并登录的 CLI。后端会用它以你的订阅额度作答，<b className="text-foreground">不用填 key</b>。
              <span className="text-muted-foreground/60">（仅当后端跑在你本机时可用；复盘 / 今日要点 / 个股问 AI 等场景。）</span>
            </p>
            {availState === "failed" && (
              <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                <span>
                  没能从后端确认哪些 CLI 可用（后端没起来？或需要填下面的访问密钥）。
                  订阅接入暂时不可选 —— 可以先用「API 接入」。
                </span>
              </div>
            )}
            <div className="grid gap-2 sm:grid-cols-2">
              {subscriptionModels.map((m) => {
                const on = cliId === m.id;
                const { ok, why } = cliState(m);
                const notInstalled = why === "本机未安装这个命令";
                return (
                  <button key={m.id} disabled={!ok} onClick={() => setCliId(m.id)}
                    className={`flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-left transition-colors ${
                      !ok
                        ? "cursor-not-allowed border-border/50 opacity-40"
                        : on
                        ? "border-primary/50 bg-primary/10"
                        : "border-border hover:bg-muted/40"
                    }`}>
                    <Terminal className={`h-4 w-4 shrink-0 ${on ? "text-primary" : "text-muted-foreground"}`} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 font-medium">
                        {m.name}
                        {m.comingSoon && <span className="rounded bg-muted/60 px-1 py-0.5 text-[9px] text-muted-foreground">即将支持</span>}
                        {/* 「没装」和「被禁用」要分开说：一个去装就行，一个别想了 */}
                        {!ok && !m.comingSoon && (
                          availState !== "ready"
                            ? <span className="rounded bg-muted/60 px-1 py-0.5 text-[9px] text-muted-foreground"
                                title={why ?? undefined}>{availState === "failed" ? "无法确认" : "检测中"}</span>
                            : notInstalled
                            ? <span className="rounded bg-muted/60 px-1 py-0.5 text-[9px] text-muted-foreground"
                                title="服务端在本机 PATH 里没找到这个命令">未安装</span>
                            : <span className="rounded bg-danger/15 px-1 py-0.5 text-[9px] font-bold text-danger"
                                title={why ?? undefined}>⛔ 已禁用</span>
                        )}
                        {on && <Check className="h-3.5 w-3.5 text-primary" />}
                      </div>
                      <div className="truncate text-[11px] text-muted-foreground">{m.description}</div>
                    </div>
                  </button>
                );
              })}
            </div>
            {}
            <div className="mt-2 flex items-start gap-2 rounded-lg border border-border bg-muted/20 p-2.5 text-[11px] leading-relaxed text-muted-foreground">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                <b>为什么只剩 Claude Code 可选</b>：其余几个 CLI 以「自动批准」方式运行，
                会<b>不经询问</b>地读写文件、执行命令；而问 AI 时
                <b>页面上下文会原样进 prompt</b> —— 页面里那些抓来的
                外部新闻与研报原文，若夹带提示注入，就能驱动它动你的文件。
                Claude Code 被限制了文件与命令工具
                （<code className="text-[10px]">--disallowedTools</code>），
                <b>功能上完全够用</b>（问 AI 是一次性作答，不需要那些工具）。
                要放开某一个：给服务端设
                <code className="text-[10px]">VIBE_ALLOW_UNSAFE_CLI=qwen</code>（可逗号分隔），
                前端不用改。
              </span>
            </div>
            <div className="flex items-center gap-2 pt-1">
              <button onClick={saveSubscription} className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25">
                保存
              </button>
              {existing && (
                <button onClick={forget} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:text-destructive">
                  <Trash2 className="h-4 w-4" /> 清除
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className="space-y-4 text-sm">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">选择模型</label>
              <select value={apiId} onChange={(e) => pickApiModel(e.target.value)}
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50">
                {apiModels.map((m) => (
                  <option key={m.id} value={m.id}>{m.name} —— {m.description}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Base URL</label>
              <input value={baseURL} onChange={(e) => setBaseURL(e.target.value)} placeholder="https://api.deepseek.com"
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Model</label>
              <input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="模型名称（豆包填 ep-… 接入点 ID）"
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">API Key</label>
              <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…"
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
            </div>

            <div className="flex items-center gap-2">
              <button onClick={saveApi} className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25">
                保存（存本地）
              </button>
              {existing && (
                <button onClick={forget} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:text-destructive">
                  <Trash2 className="h-4 w-4" /> 清除
                </button>
              )}
            </div>
          </div>
        )}
      </GlassCard>

      {/* 后端访问密钥：仅当后端部署时设置了 VR_API_KEY（公网防蹭用）才需要填 */}
      <GlassCard className="mt-4">
        <h3 className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
          <KeyRound className="h-4 w-4 text-primary" /> 后端访问密钥（可选）
        </h3>
        <p className="mb-3 text-xs text-muted-foreground">
          仅当后端部署时设置了 <code className="rounded bg-muted/50 px-1">VR_API_KEY</code>（公网部署防蹭用）才需要填，填后端同一个值；
          本机自用没设鉴权就留空。同样只存本地浏览器。
        </p>
        <div className="flex items-center gap-2">
          <input type="password" value={accessKey} onChange={(e) => setAccessKey(e.target.value)} placeholder="与后端 VR_API_KEY 保持一致"
            className="flex-1 rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
          <button onClick={saveAccess} className="rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary hover:bg-primary/25">
            保存
          </button>
        </div>
      </GlassCard>
    </div>
  );
}
