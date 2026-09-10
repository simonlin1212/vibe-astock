import { useEffect, useRef, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { ChevronsLeft, ChevronsRight, Globe, Github, Cog, Menu, Moon, Sparkles, Sun, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useDarkMode } from '@/hooks/useDarkMode';
import { moduleFor, chatPageFor, navigationPath } from '@/lib/workspace/modules';
import { ModuleNav } from '@/components/workspace/ModuleNav';
import { WorkspaceProvider, useWorkspace } from '@/lib/workspace/state';
import { AgentToggle } from '@/components/workspace/AgentToggle';
import { PhoenixTreeLogo } from '@/components/workspace/PhoenixTreeLogo';
import { QuickAiConnect } from '@/components/workspace/QuickAiConnect';
import { WorkspaceChat } from '@/components/workspace/WorkspaceChat';
import { Dialog } from '@/components/workspace/Dialog';
import product from "../../../../product.json";
const APP_VERSION = `v${product.version}`;
function readCollapsed() { try { return localStorage.getItem('va-sidebar') === 'collapsed'; } catch { return false; } }
function Shell() {
  const { pathname, search } = useLocation();
  const state = useWorkspace();
  const { dark, toggle } = useDarkMode();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [storageError, setStorageError] = useState('');
  const navRef = useRef<HTMLElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const activeModule = moduleFor(pathname);
  const title = activeModule?.pages.find(page => page.to === navigationPath(pathname))?.title ?? (pathname === '/settings' ? '接入 AI' : '工作空间');
  useEffect(() => { setMobileOpen(false); setChatOpen(false); }, [pathname, search]);
  useEffect(() => {
    const query = window.matchMedia('(min-width: 768px)');
    const close = () => { if (query.matches) setMobileOpen(false); };
    query.addEventListener('change', close); return () => query.removeEventListener('change', close);
  }, []);
  useEffect(() => { navRef.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest' }); }, [pathname, collapsed]);
  const collapse = () => {
    try { localStorage.setItem('va-sidebar', collapsed ? 'expanded' : 'collapsed'); setCollapsed(!collapsed); setStorageError(''); }
    catch { setStorageError('无法保存侧栏选择。'); }
  };
  const sidebar = (compact: boolean, mobile = false) => <>
    <div className={cn('border-b border-border', compact ? 'px-2 py-5' : 'px-5 py-4')}>
      <div className="flex items-center justify-between gap-1"><Link to="/" aria-label="Vibe AStock 首页" className="flex items-center gap-2.5"><PhoenixTreeLogo className="h-8 w-6 shrink-0 text-primary" />{!compact && <span className="workspace-brand text-lg font-semibold tracking-tight">Vibe-<span className="text-primary">AStock</span></span>}</Link>
        {mobile && <button type="button" onClick={() => setMobileOpen(false)} aria-label="关闭导航" className="p-2"><X className="h-4 w-4" /></button>}</div>
      {!compact && <div data-ai-identity className="mt-2 space-y-1"><p className="text-[10px] leading-4 text-muted-foreground">A 股短线复盘与跟踪工作台</p><button type="button" onClick={state.connect} title={state.label} className="flex min-w-0 items-start gap-1 text-left text-[10px] leading-5 text-muted-foreground hover:text-primary"><span aria-hidden="true" className="mt-2 h-1 w-1 shrink-0 rounded-full bg-primary" /><span>{state.previouslyTested && (state.status === 'authenticated' || state.status === 'saved') ? `已接入AI：${state.name}` : state.label}</span></button><AgentToggle /></div>}
      {compact && <div className="mt-3 flex justify-center"><AgentToggle compact /></div>}
    </div>
    <nav ref={mobile ? undefined : navRef} aria-label="产品导航" className={cn('min-h-0 flex-1 space-y-0.5 overflow-auto py-3', compact ? 'px-1.5' : 'px-3')}>
      <ModuleNav compact={compact} expandSidebar={collapse} navigate={() => {
        if (mobile) { setMobileOpen(false); requestAnimationFrame(() => mainRef.current?.focus()); }
      }} />
    </nav>
    <div className={cn('border-t border-border', compact ? 'p-1.5' : 'p-3')}>
      <Link to="/settings" title="接入 AI" aria-label="接入 AI" aria-current={pathname === '/settings' ? 'page' : undefined} onClick={() => setMobileOpen(false)} className={cn('mb-2 flex min-h-10 items-center gap-2 rounded-lg text-sm text-muted-foreground hover:bg-muted hover:text-foreground', compact ? 'justify-center' : 'px-3')}><Cog className="h-4 w-4" />{!compact && '接入 AI'}</Link>
      <div className={cn('flex items-center text-muted-foreground', compact ? 'flex-col gap-3' : 'justify-between gap-2')}>
        <a href="https://phoenixtree.ai/" target="_blank" rel="noopener noreferrer" aria-label="Phoenix Tree AI 官网（新标签页打开）" title="Phoenix Tree AI 官网" className="flex min-h-9 items-center gap-1.5 whitespace-nowrap rounded text-xs text-primary"><Globe className="h-3.5 w-3.5 shrink-0" />{!compact && <span>phoenixtree.ai</span>}</a>
        <a href="https://x.com/linsizhen" target="_blank" rel="noopener noreferrer" aria-label="X · 联系作者" title="X @linsizhen" className="p-1 hover:text-foreground"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className="h-3.5 w-3.5"><path d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.64 7.584H.47l8.6-9.835L0 1.154h7.594l5.243 6.932 6.064-6.933ZM17.61 20.644h2.039L6.486 3.24H4.298L17.61 20.644Z" /></svg></a>
        <a href="https://github.com/simonlin1212/vibe-astock" target="_blank" rel="noopener noreferrer" aria-label="GitHub" title="GitHub" className="p-1 hover:text-foreground"><Github className="h-3.5 w-3.5" /></a>
        {!mobile && <button type="button" onClick={collapse} aria-label={compact ? '展开侧栏' : '收起侧栏'} className="p-1">{compact ? <ChevronsRight className="h-3.5 w-3.5" /> : <ChevronsLeft className="h-3.5 w-3.5" />}</button>}
      </div>{storageError && <p role="alert" className="text-xs text-destructive">{storageError}</p>}
    </div>
  </>;
  return <>
    <a className="workspace-skip" href="#workspace-main" onClick={e => { e.preventDefault(); mainRef.current?.focus(); }}>跳到内容</a>
    <div className="flex h-dvh overflow-hidden">
      <aside aria-label="产品侧栏" className={cn('workspace-sidebar hidden shrink-0 flex-col md:flex', collapsed ? 'w-16' : 'w-64')}>{sidebar(collapsed)}</aside>
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="workspace-topbar flex min-h-16 shrink-0 items-center justify-between gap-2 px-4 md:px-8">
          <div className="flex min-w-0 items-center gap-3 text-xs"><button type="button" aria-label="打开导航" onClick={() => setMobileOpen(true)} className="p-2 md:hidden"><Menu className="h-4 w-4" /></button><span className="hidden text-muted-foreground sm:inline">工作空间 /</span><strong className="truncate font-medium">{title}</strong></div>
          <div className="flex shrink-0 items-center gap-2"><span className="hidden text-[10px] text-muted-foreground lg:inline">{APP_VERSION}</span><button type="button" onClick={toggle} aria-label={dark ? '切换为浅色' : '切换为深色'} className="rounded p-2 text-muted-foreground hover:bg-muted">{dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}</button>{pathname !== '/' && <button type="button" onClick={() => setChatOpen(true)} className="ai-chat-trigger inline-flex items-center gap-1.5 rounded-full px-3 py-2 text-xs"><Sparkles className="h-4 w-4" />{state.enabled ? '问 Agent' : '问模型'}</button>}</div>
        </header>
        <main ref={mainRef} id="workspace-main" tabIndex={-1} className="min-h-0 flex-1 overflow-auto"><div className="workspace-content"><Outlet /></div></main>
      </div>
    </div>
    {mobileOpen && <Dialog titleId="mobile-nav-title" close={() => setMobileOpen(false)} className="max-w-xs"><h2 id="mobile-nav-title" className="sr-only">产品导航</h2><div className="workspace-sidebar flex h-[85dvh] flex-col">{sidebar(false, true)}</div></Dialog>}
    {chatOpen && pathname !== '/' && <Dialog titleId="page-chat-title" close={() => setChatOpen(false)} className="max-w-2xl"><div className="flex items-center justify-between px-5 py-3"><h2 id="page-chat-title" className="text-sm font-semibold">页面对话</h2><button type="button" onClick={() => setChatOpen(false)} aria-label="关闭页面对话" className="p-2"><X className="h-4 w-4" /></button></div><WorkspaceChat key={pathname + search} page={chatPageFor(pathname)} /></Dialog>}
    {state.setupOpen && <QuickAiConnect />}
  </>;
}
export function Layout() { return <WorkspaceProvider><Shell /></WorkspaceProvider>; }
