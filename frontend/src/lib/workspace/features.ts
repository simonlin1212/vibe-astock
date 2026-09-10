// Research v1.1.0 compact category structure; AStock routes remain authoritative.
import { Activity, Flame, CalendarRange, Swords, Eye, Sunrise, LineChart, Microscope, Radio, Briefcase, NotebookPen, TrendingDown, Cog } from 'lucide-react';
export const FEATURE_GROUPS = [
  { title: '复盘与情绪', features: [
    { to: '/agent/review', title: '复盘看板', icon: Swords, detail: '完整复盘、依据与报告历史' },
    { to: '/daily-review', title: '盘面数据', icon: Activity, detail: '情绪指标与市场数据' },
    { to: '/first-board', title: '首板分析', icon: Flame, detail: '首板样本与结构' },
    { to: '/heat', title: '近5天热度', icon: CalendarRange, detail: '已有归档的热度变化' },
  ] },
  { title: '盯盘与核验', features: [
    { to: '/watch', title: '盯盘', icon: Eye, detail: '查看关注标的的盘中表现' },
    { to: '/agent/intraday', title: '复盘验证', icon: Sunrise, detail: '竞价与盘中路径核验' },
  ] },
  { title: '标的与资讯', features: [
    { to: '/stock-data', title: '个股研究', icon: LineChart, detail: '行情、公告与公开资料' },
    { to: '/agent/deepdive', title: '多空辩论', icon: Microscope, detail: '四类分析与多空双方证据论证' },
    { to: '/intel', title: '资讯雷达', icon: Radio, detail: '公开资讯与行业线索' },
  ] },
  { title: '记录与验证', features: [
    { to: '/my-stocks', title: '持仓自选', icon: Briefcase, detail: '持仓与关注标的，本机私人记录' },
    { to: '/notes', title: '研究记录', icon: NotebookPen, detail: '已保存的分析，支持搜索与导出' },
    { to: '/journal', title: '交易日志', icon: NotebookPen, detail: '私人记录，不自动进入聊天' },
    { to: '/backtest', title: '涨停样本统计', icon: TrendingDown, detail: '历史样本统计，不是完整收益回测' },
  ] },
  { title: '工作台设置', features: [
    { to: '/settings', title: '接入 AI', icon: Cog, detail: '订阅、API 与接入状态' },
  ] },
] as const;
export const FEATURES = FEATURE_GROUPS.flatMap(group => [...group.features]);
export const SUBSCRIPTIONS = [
  { id: 'codex-private', label: 'Codex订阅版', detail: '产品专用登录 · 已有复盘与追问接入' },
  { id: 'claude', label: 'Claude订阅', detail: '本机 Claude Code · 官方订阅登录' },
  { id: 'codebuddy', label: 'WorkBuddy CLI', detail: 'WorkBuddy 内置 CodeBuddy · macOS 已完成单日复盘流程实测' },
] as const;
