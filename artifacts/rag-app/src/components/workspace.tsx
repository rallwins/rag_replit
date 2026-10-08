import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { Link, useLocation } from 'wouter';
import { BookOpen, Boxes, ChevronRight, CircleHelp, Database, FileText, Gauge, Menu, Settings2, X } from 'lucide-react';
import { useListCollections } from '@workspace/api-client-react';
import type { Document, ActivityEvent } from '@workspace/api-client-react';

export function Shell({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  const [mobileNav, setMobileNav] = useState(false);
  const nav = [
    { href: '/', label: 'Workspace overview', icon: Gauge },
    { href: '/settings', label: 'System readiness', icon: Settings2 },
  ];
  return <div className="app-shell">
    <aside className="sidebar">
      <Link href="/" className="brand"><span className="brand-mark"><BookOpen size={18}/></span><span><span className="brand-name">Fieldnotes</span><span className="brand-caption">Knowledge workspace</span></span></Link>
      <div className="nav-label">Workspace</div>
      {nav.map(item => <Link key={item.href} href={item.href} className={`nav-link ${location === item.href ? 'active' : ''}`}><item.icon size={15}/>{item.label}</Link>)}
      <div className="side-collections"><div className="nav-label">Collections</div><CollectionNav /></div>
      <div className="side-bottom"><div className="workspace-avatar">WK</div><div><div className="side-user-name">Research workspace</div><div className="side-user-caption">Private team space</div></div></div>
    </aside>
    <div className="main-area">
      <header className="topbar">
        <div style={{display:'flex',alignItems:'center',gap:10}}>
          <button className="top-icon mobile-menu" aria-label="Open navigation" onClick={()=>setMobileNav(true)}><Menu size={15}/></button>
          <div className="breadcrumbs">WORKSPACE <ChevronRight size={11} style={{display:'inline',verticalAlign:'middle',margin:'0 4px'}}/> <strong>{location === '/settings' ? 'SYSTEM READINESS' : location.startsWith('/collections/') ? 'COLLECTION' : 'OVERVIEW'}</strong></div>
        </div>
        <div className="top-right"><div className="health-pill"><span className="health-dot"/> Service status</div><Link href="/settings" className="top-icon" aria-label="System settings"><Settings2 size={14}/></Link></div>
      </header>
      <main className="content">{children}</main>
    </div>
    {mobileNav && <div className="overlay" onMouseDown={e=>{if(e.target===e.currentTarget)setMobileNav(false)}}><div className="dialog" style={{maxWidth:330,marginRight:'auto',height:'100%',borderRadius:0}}><div className="dialog-head"><span className="brand-name">Fieldnotes</span><button className="dialog-close" aria-label="Close navigation" onClick={()=>setMobileNav(false)}><X size={18}/></button></div>{nav.map(item=><Link onClick={()=>setMobileNav(false)} key={item.href} href={item.href} className="nav-link" style={{color:'#3d575a'}}><item.icon size={15}/>{item.label}</Link>)}<CollectionNav /></div></div>}
  </div>;
}

function CollectionNav() {
  const list = useListCollections({limit:100});
  if (list.isLoading) return <div style={{padding:'8px 12px'}}><div className="skeleton" style={{width:'76%',marginBottom:9}}/><div className="skeleton" style={{width:'58%'}}/></div>;
  return <>{(list.data?.items ?? []).slice(0,7).map(c=><Link key={c.id} href={`/collections/${c.id}`} className="side-collection"><span className="side-dot"/>{c.name}</Link>)}{!list.data?.items?.length && <div style={{padding:'8px 12px',fontSize:11,color:'#9aafaa'}}>No collections yet</div>}</>;
}

export function Button({children,onClick,variant='primary',disabled,type='button',className='',form}:{children:ReactNode;onClick?:()=>void;variant?:'primary'|'outline'|'quiet'|'danger';disabled?:boolean;type?:'button'|'submit';className?:string;form?:string}) {
  return <button type={type} form={form} disabled={disabled} onClick={onClick} className={`button button-${variant} ${className}`}>{children}</button>;
}

export function Modal({title,children,onClose,footer}:{title:string;children:ReactNode;onClose:()=>void;footer?:ReactNode}) {
  return <div className="overlay" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)onClose()}}><section className="dialog" role="dialog" aria-modal="true" aria-label={title}><div className="dialog-head"><h2 className="dialog-title">{title}</h2><button className="dialog-close" onClick={onClose} aria-label="Close dialog"><X size={18}/></button></div>{children}{footer && <div className="dialog-actions">{footer}</div>}</section></div>;
}

export function useNotice() {
  const [notice,setNotice] = useState('');
  useEffect(()=>{if(!notice)return;const timer=window.setTimeout(()=>setNotice(''),3200);return()=>window.clearTimeout(timer)},[notice]);
  const toast = notice ? <div className="toast-message" role="status">{notice}</div> : null;
  return {showNotice:setNotice,toast};
}

export function SkeletonBlock({rows=4}:{rows?:number}) {
  return <div className="section-panel" style={{padding:22,display:'grid',gap:15}} aria-label="Loading"><div className="skeleton" style={{width:'34%',height:18}}/>{Array.from({length:rows},(_,i)=><div key={i} className="skeleton" style={{width:`${92-i*8}%`}}/>)}</div>;
}

export function ErrorState({message,retry}:{message:string;retry:()=>void}) {
  return <div className="error-panel"><CircleHelp size={22}/><p style={{fontSize:13,margin:'10px 0'}}>{message}</p><Button variant="outline" onClick={retry}>Try again</Button></div>;
}

export function EmptyState({title,description,action}:{title:string;description:string;action?:ReactNode}) {
  return <div className="empty-state"><div className="empty-icon"><Database size={19}/></div><h3>{title}</h3><p>{description}</p>{action}</div>;
}

export function StatusBadge({status}:{status:Document['status']}) {
  return <span className={`status-badge status-${status}`}><span className="legend-dot" style={{background:status==='ready'?'#638d75':status==='failed'?'#b96552':status==='processing'||status==='queued'?'#d6b65e':'#969e97'}}/>{status}</span>;
}

export function FileGlyph({mediaType}:{mediaType:string}) {
  return <span className="file-icon" aria-hidden="true" title={mediaType}><FileText size={14}/></span>;
}

export function formatBytes(bytes:number) {
  if(bytes<1024)return `${bytes} B`;
  if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} KB`;
  return `${(bytes/(1024*1024)).toFixed(1)} MB`;
}

export function relativeTime(date:string) {
  const then=new Date(date).getTime(), delta=Math.max(0,Date.now()-then);
  const mins=Math.floor(delta/60000);
  if(mins<1)return 'just now';
  if(mins<60)return `${mins}m ago`;
  const hours=Math.floor(mins/60);if(hours<24)return `${hours}h ago`;
  const days=Math.floor(hours/24);if(days<7)return `${days}d ago`;
  return new Date(date).toLocaleDateString(undefined,{month:'short',day:'numeric'});
}

export function activityLabel(event:ActivityEvent) {
  const type=event.event_type.replace(/[_-]/g,' ').toLowerCase();
  const resource=event.resource_type?.toLowerCase() || 'resource';
  return {type,resource};
}

export function DialogForm({children,onSubmit}:{children:ReactNode;onSubmit:(event:FormEvent<HTMLFormElement>)=>void}) {
  return <form onSubmit={onSubmit}>{children}</form>;
}

export function CollectionMark() { return <span className="collection-icon"><Boxes size={17}/></span>; }
