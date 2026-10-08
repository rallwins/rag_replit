import { useState, type FormEvent } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Link, useLocation } from 'wouter';
import { Activity, ArrowUpRight, BookMarked, CirclePlus, FileStack, RefreshCw } from 'lucide-react';
import {
  getGetDashboardQueryKey, getListActivityQueryKey, getListCollectionsQueryKey,
  useCreateCollection, useGetDashboard, useListActivity, useListCollections,
} from '@workspace/api-client-react';
import { activityLabel, Button, CollectionMark, EmptyState, ErrorState, Modal, relativeTime, SkeletonBlock, useNotice } from '@/components/workspace';

export default function HomePage() {
  const queryClient = useQueryClient();
  const [,setLocation] = useLocation();
  const dashboard = useGetDashboard();
  const collectionQuery = useListCollections({limit:100});
  const activityQuery = useListActivity({limit:8});
  const createCollection = useCreateCollection();
  const {showNotice,toast} = useNotice();
  const [createOpen,setCreateOpen] = useState(false);
  const [name,setName] = useState('');
  const [description,setDescription] = useState('');
  const [filter,setFilter] = useState('');
  const collections = collectionQuery.data?.items ?? [];
  const filtered = collections.filter(c=>`${c.name} ${c.description}`.toLowerCase().includes(filter.toLowerCase()));
  const refresh = () => { dashboard.refetch(); collectionQuery.refetch(); activityQuery.refetch(); };
  const submit = (event:FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if(!name.trim())return;
    createCollection.mutate({data:{name:name.trim(),description:description.trim()}},{
      onSuccess: async c => {
        await Promise.all([
          queryClient.invalidateQueries({queryKey:getListCollectionsQueryKey({limit:100})}),
          queryClient.invalidateQueries({queryKey:getGetDashboardQueryKey()}),
          queryClient.invalidateQueries({queryKey:getListActivityQueryKey({limit:8})}),
        ]);
        setCreateOpen(false);setName('');setDescription('');showNotice('Collection created');
        setLocation(`/collections/${c.id}`);
      },
      onError:()=>showNotice('Collection could not be created. Try again.'),
    });
  };
  if(dashboard.isLoading && !dashboard.data) return <><div className="page-head"><div><div className="eyebrow">RESEARCH LIBRARY / 01</div><h1 className="page-title">Workspace overview</h1></div></div><SkeletonBlock rows={6}/></>;
  if(dashboard.isError && !dashboard.data) return <ErrorState message="Workspace overview is temporarily unavailable." retry={refresh}/>;
  const data=dashboard.data;
  const total=Math.max(1,data?.document_count ?? 0);
  const successEvents=activityQuery.data?.items ?? data?.recent_activity ?? [];
  return <>
    <div className="page-head"><div><div className="eyebrow">RESEARCH LIBRARY / 01</div><h1 className="page-title">Workspace overview</h1><p className="page-subtitle">A clear view of what your team knows, and what is still being indexed.</p></div><Button onClick={()=>setCreateOpen(true)}><CirclePlus size={15}/> New collection</Button></div>
    {dashboard.isError && <div className="notice" style={{marginBottom:16}}>Live overview data could not refresh. Showing the most recently available snapshot.</div>}
    <section className="stat-grid" aria-label="Workspace counts">
      <Stat label="Collections" value={data?.collection_count ?? 0} foot="maintained knowledge spaces"/>
      <Stat label="Documents" value={data?.document_count ?? 0} foot="across all collections"/>
      <Stat label="Ready to retrieve" value={data?.ready_document_count ?? 0} foot={data?.document_count ? `${Math.round((data.ready_document_count/data.document_count)*100)}% indexed and available` : 'awaiting your first source'}/>
      <Stat label="Needs attention" value={data?.failed_document_count ?? 0} foot={`${data?.processing_document_count ?? 0} currently processing`} attention={(data?.failed_document_count ?? 0)>0}/>
    </section>
    <div className="overview-grid">
      <section className="section-panel">
        <div className="section-heading"><div><h2 className="section-title">Document pipeline</h2><div className="section-kicker" style={{marginTop:4}}>INGESTION STATUS ACROSS YOUR WORKSPACE</div></div><button className="icon-button" aria-label="Refresh workspace data" onClick={refresh}><RefreshCw size={13}/></button></div>
        {data ? <div className="pipeline"><div className="pipeline-top"><span>{data.document_count} total documents</span><span>{data.ready_document_count} ready</span></div><div className="pipeline-bar" role="img" aria-label={`${data.ready_document_count} ready, ${data.processing_document_count} processing, ${data.failed_document_count} failed`}>
          <span className="seg-ready" style={{width:`${data.ready_document_count/total*100}%`}}/><span className="seg-processing" style={{width:`${data.processing_document_count/total*100}%`}}/><span className="seg-failed" style={{width:`${data.failed_document_count/total*100}%`}}/>
        </div><div className="pipeline-legend"><Legend color="#638d75" label="Ready" value={data.ready_document_count}/><Legend color="#d6b65e" label="Processing" value={data.processing_document_count}/><Legend color="#b96552" label="Failed" value={data.failed_document_count}/></div></div> : <div className="empty-state"><h3>Pipeline is clear</h3><p>Counts will appear when workspace data is available.</p></div>}
      </section>
      <section className="section-panel">
        <div className="section-heading"><div><h2 className="section-title">Recent activity</h2><div className="section-kicker" style={{marginTop:4}}>CONTENT-FREE AUDIT TRAIL</div></div><Activity size={15} color="#73857e"/></div>
        {activityQuery.isLoading ? <div style={{padding:20}}><SkeletonBlock rows={3}/></div> : successEvents.length ? <div className="activity-list">{successEvents.slice(0,6).map(ev=>{const {type,resource}=activityLabel(ev);return <div className="activity-item" key={ev.id}><span className="activity-symbol"><FileStack size={13}/></span><div className="activity-description"><strong>{type}</strong><br/>{resource} · <span style={{color:ev.outcome==='success'?'#63816c':'#a65645'}}>{ev.outcome}</span></div><time className="activity-time">{relativeTime(ev.created_at)}</time></div>})}</div>
          : <div className="empty-state"><h3>Nothing recorded yet</h3><p>Collection and document changes will leave a short audit trail here.</p></div>}
      </section>
    </div>
    <section style={{marginTop:30}}>
      <div className="page-head" style={{marginBottom:15,alignItems:'center',flexDirection:'row'}}><div><div className="eyebrow">MAINTAINED SOURCES</div><h2 className="section-title" style={{fontSize:24,marginTop:5}}>Your collections</h2></div><div style={{display:'flex',alignItems:'center',gap:12}}><span className="section-kicker">{collections.length} COLLECTIONS</span></div></div>
      {collectionQuery.isError && !collectionQuery.data && <ErrorState message="Collections could not be loaded." retry={()=>collectionQuery.refetch()}/>}
      {collectionQuery.isLoading ? <div className="collection-grid">{[1,2,3].map(n=><SkeletonBlock key={n} rows={3}/>)}</div> : collections.length ? <>
        <div className="collection-toolbar"><div className="search-wrap"><BookMarked size={15}/><input className="search-input" value={filter} onChange={e=>setFilter(e.target.value)} placeholder="Find a collection" aria-label="Find a collection" data-testid="input-collection-search"/></div><span className="section-kicker">{filtered.length} SHOWN</span></div>
        {filtered.length ? <div className="collection-grid">{filtered.map(c=><Link key={c.id} href={`/collections/${c.id}`} className="collection-card" data-testid={`card-collection-${c.id}`}><div className="collection-card-top"><CollectionMark/><span className="collection-count">{c.document_count} {c.document_count===1?'DOCUMENT':'DOCUMENTS'}</span></div><h3>{c.name}</h3><p>{c.description || 'A maintained source collection for your team.'}</p><div className="collection-card-foot"><span>UPDATED {relativeTime(c.updated_at).toUpperCase()}</span><ArrowUpRight size={14}/></div></Link>)}</div>
          : <EmptyState title="No collections match" description="Try another name or clear the search." action={<Button variant="outline" onClick={()=>setFilter('')}>Clear search</Button>}/>}
      </> : <EmptyState title="Start with a source collection" description="Organize your maintained documents into a focused knowledge space. Answers and retrieval stay scoped to each collection." action={<Button onClick={()=>setCreateOpen(true)}><CirclePlus size={14}/> Create collection</Button>}/>}
    </section>
    {createOpen && <Modal title="Create a collection" onClose={()=>setCreateOpen(false)} footer={<><Button variant="outline" onClick={()=>setCreateOpen(false)}>Cancel</Button><Button type="submit" form="create-collection" disabled={!name.trim()||createCollection.isPending}>{createCollection.isPending?'Creating…':'Create collection'}</Button></>}>
      <form id="create-collection" onSubmit={submit}><div className="field"><label className="label" htmlFor="collection-name">Collection name</label><input id="collection-name" autoFocus maxLength={120} required value={name} onChange={e=>setName(e.target.value)} placeholder="e.g. Product field research"/></div><div className="field"><label className="label" htmlFor="collection-description">Purpose <span style={{fontWeight:400,color:'#929b94'}}>optional</span></label><textarea id="collection-description" maxLength={1000} value={description} onChange={e=>setDescription(e.target.value)} placeholder="What sources belong here, and who relies on them?"/></div></form>
    </Modal>}
    {toast}
  </>;
}

function Stat({label,value,foot,attention=false}:{label:string;value:number;foot:string;attention?:boolean}) {
  return <div className="stat-card"><div className="stat-card-label">{label}</div><div className="stat-card-value" style={attention&&value>0?{color:'#a65645'}:{}}>{value}</div><div className="stat-card-foot">{foot}</div></div>;
}
function Legend({color,label,value}:{color:string;label:string;value:number}) { return <span className="legend-item"><span className="legend-dot" style={{background:color}}/>{label} <b style={{color:'#4a605a',fontWeight:600}}>{value}</b></span> }
