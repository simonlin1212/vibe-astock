import { randomId, hexHash } from "./random-id";
// Every product AI entry reads the same explicitly tested connection.
import { agentRequest, AgentRequestError, loadAgentConnection } from './agent-api';
import { readMode } from './workspace/state';
import { ApiError } from './api';
import { toast } from 'sonner';
export interface ChatMsg { role: 'user' | 'assistant'; content: string }
export interface ChatResult { content: string; trace: {tool: string; args: Record<string, unknown>}[]; rounds: number; ai_source?: {provider: string; model: string} }
export interface ChatHandlers { sessionId?: string; onDelta?: (text: string) => void; onTool?: (tool: string, args: Record<string, unknown>) => void }
export function hasLlm() { return loadAgentConnection() !== null; }
type Job = {job_id: string; running: boolean; status: string; error?: string; trace?: ChatResult['trace']; result?: ChatResult};
function aborted() { return new DOMException('已取消', 'AbortError'); }
async function delay(signal?: AbortSignal) {
  if (signal?.aborted) throw aborted();
  await new Promise<void>((resolve, reject) => {
    const cancel = () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); reject(aborted()); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', cancel); resolve(); }, 500);
    signal?.addEventListener('abort', cancel, {once:true});
  });
}
export async function chatStream(messages: ChatMsg[], context: string, handlers: ChatHandlers = {}, signal?: AbortSignal): Promise<ChatResult> {
  const llm = loadAgentConnection();
  if (!llm) throw new ApiError('尚未接入 AI，请先在「接入 AI」中完成测试保存', 400);
  if (signal?.aborted) throw aborted();
  const latest = messages[messages.length - 1];
  if (!latest || latest.role !== 'user' || latest.content.length > 4000 || context.length > 8000) {
    throw new ApiError('本次问题或材料过长，请缩小选中内容后再试', 400);
  }
  // Five previous exchanges at most; a long old answer is explicitly an excerpt.
  const windowed = messages.slice(-11).map((m,i,a)=>({...m, content:i===a.length-1?m.content:
    m.content.length>1000 ? m.content.slice(0,950)+'\n[较早消息已节选，请勿推断省略内容]' : m.content}));
  const input = {messages:windowed, context, allow_tools: readMode(), llm};
  // Recover by source + visible material + current question, even when a caller
  // retained an optimistic user/error bubble after transport failure.
  const identity = {session:handlers.sessionId ?? "single-question", question:latest.content, context, allow_tools:input.allow_tools, llm};
  const hash = hexHash(JSON.stringify(identity));
  const pendingKey = 'astock-page-chat-' + hash;
  let saved: string | null;
  try { saved = sessionStorage.getItem(pendingKey); } catch { throw new ApiError('浏览器禁止会话存储，本次尚未发送 AI 请求；请允许存储后重试', 400); }
  const forget = () => { try { sessionStorage.removeItem(pendingKey); } catch { toast.warning('回答状态未能从浏览器清除；请保留本次结果，勿重复提交'); } };
  let pending: {request_id:string; input:Omit<typeof input,'llm'>};
  if (saved) {
    try { pending = JSON.parse(saved); if(!/^[a-f0-9]{32}$/.test(pending.request_id)||!pending.input)throw new Error(); }
    catch { throw new ApiError('待恢复请求记录损坏，请重新打开页面检查任务状态',400); }
  } else {
    const {llm: _llm, ...snapshot} = input;
    pending = {request_id:randomId().replace(/-/g,''), input:snapshot};
    try { sessionStorage.setItem(pendingKey, JSON.stringify(pending)); } catch { throw new ApiError('浏览器未能保存恢复记录，本次尚未发送 AI 请求；请释放存储空间后重试', 400); }
  }
  const {request_id} = pending;
  const body = {...pending.input, llm, request_id};
  let seen = 0;
  let cancellation: Promise<void> | undefined;
  const confirmCancel = async () => {
    try {
      const result = await agentRequest<Job>(`/chat-jobs/${request_id}/cancel`, {});
      if (!result.running && result.status === 'cancelled') forget();
      else if (result.running) toast.info('正在取消；模型和取数进程将停止，请稍候');
      else toast.info('这次回答已完成；再次发送同一问题可恢复已有结果');
    } catch {
      toast.error('取消尚未确认，后台任务可能仍在执行。可重试取消；任务达到时限也会停止。', {
        duration:15000, action:{label:'重试取消', onClick:()=>{void confirmCancel();}}
      });
    }
  };
  const cancel = () => { cancellation = confirmCancel(); };
  signal?.addEventListener('abort', cancel, {once:true});
  try {
    if (signal?.aborted) { cancel(); throw aborted(); }
    let job: Job;
    try { job = await agentRequest<Job>('/chat-jobs', body, signal); }
    catch (error) {
      if (signal?.aborted) throw aborted();
      if (error instanceof AgentRequestError && error.status < 500) throw error;
      // A transport retry carries the identical id/body, never a fresh paid request.
      job = await agentRequest<Job>('/chat-jobs', body, signal);
    }
    for (;;) {
      if (signal?.aborted) throw aborted();
      for (const item of (job.trace || []).slice(seen)) handlers.onTool?.(item.tool, item.args);
      seen = (job.trace || []).length;
      if (!job.running) {
        forget();
        if (job.status !== 'complete' || !job.result) throw new ApiError(job.error || '本次回答未完成', 502);
        handlers.onDelta?.(job.result.content);
        return job.result;
      }
      await delay(signal);
      job = await agentRequest<Job>(`/chat-jobs/${request_id}`, undefined, signal);
    }
  } catch (error) {
    if (error instanceof AgentRequestError && error.status >= 400 && error.status < 500) forget();
    if (signal?.aborted) { if (!cancellation) cancel(); await cancellation; }
    throw error;
  } finally { signal?.removeEventListener('abort', cancel); }
}
export function chat(messages: ChatMsg[], context: string) { return chatStream(messages, context); }
